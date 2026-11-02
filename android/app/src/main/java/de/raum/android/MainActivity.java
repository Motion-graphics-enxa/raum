package de.raum.android;

import android.Manifest;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.print.PrintAttributes;
import android.print.PrintManager;
import android.provider.MediaStore;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.webkit.GeolocationPermissions;
import android.webkit.PermissionRequest;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.SslErrorHandler;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.ConnectException;
import java.net.HttpURLConnection;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.net.UnknownHostException;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import javax.net.ssl.SSLException;

/**
 * RAUM on Android (plan F-09): the box's own web app in a WebView. No second UI.
 *
 * <p>Guard rails: the WebView shows the verified box origin only (other links go to the browser);
 * the microphone is granted to that origin only (after the OS permission); TLS uses the system
 * and user CA stores (res/xml/network_security_config.xml), never "proceed anyway".
 */
public final class MainActivity extends Activity {
    private static final String PREFS = "raum";
    private static final String KEY_BOX = "box";
    private static final String PRINT_URL = "raum-shell://print";
    private static final String AUDIO = PermissionRequest.RESOURCE_AUDIO_CAPTURE;
    private static final int REQ_MIC = 1;
    private static final int REQ_FILE = 2;
    private static final int TIMEOUT_MS = 6000;
    private static final int BODY_LIMIT = 64 * 1024;

    /** Android's WebView ignores window.print(); hand it to Android printing (Summary: print / PDF). */
    private static final String PRINT_SHIM = "window.print=function(){location.href='" + PRINT_URL + "';};";

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService io = Executors.newSingleThreadExecutor();

    private SharedPreferences prefs;
    private WebView web;
    private View setup;
    private EditText address;
    private Button connect;
    private TextView note;
    private View status;
    private TextView statusText;
    private TextView statusDetail;

    /** The verified box origin: the only origin the WebView shows and grants the microphone to. */
    private String boxOrigin;
    /** A box path waiting for the connection (from a link). */
    private String pendingPath;
    private PermissionRequest pendingMic;
    private ValueCallback<Uri[]> fileCallback;
    private Uri cameraShot;
    /** Results of superseded health checks are ignored. */
    private int attempt;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);

        web = findViewById(R.id.web);
        setup = findViewById(R.id.setup);
        address = findViewById(R.id.address);
        connect = findViewById(R.id.connect);
        note = findViewById(R.id.note);
        status = findViewById(R.id.status);
        statusText = findViewById(R.id.status_text);
        statusDetail = findViewById(R.id.status_detail);

        connect.setOnClickListener(v -> connect(address.getText().toString(), pendingPath));
        address.setOnEditorActionListener((v, actionId, event) -> {
            if (actionId != EditorInfo.IME_ACTION_GO) return false;
            connect.performClick();
            return true;
        });
        configureWebView();
        clearShots();

        String saved = prefs.getString(KEY_BOX, null);
        if (saved != null) address.setText(saved);
        if (!handleLink(getIntent())) {
            if (saved != null) connect(saved, null);
            else showSetup();
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleLink(intent);
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (setup.getVisibility() == View.VISIBLE) {
            if (boxOrigin != null && web.getUrl() != null) {
                showWeb();
                return;
            }
        } else if (web.canGoBack()) {
            web.goBack();
            return;
        }
        super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        io.shutdownNow();
        if (pendingMic != null) pendingMic.deny();
        if (fileCallback != null) fileCallback.onReceiveValue(null);
        ViewGroup parent = (ViewGroup) web.getParent();
        if (parent != null) parent.removeView(web);
        web.destroy();
        clearShots();
        super.onDestroy();
    }

    // ------------------------------------------------------------------ box address and links

    /** @return true if the intent carried a link this method handled */
    private boolean handleLink(Intent intent) {
        Uri data = intent == null ? null : intent.getData();
        if (data == null || !Intent.ACTION_VIEW.equals(intent.getAction())) return false;
        BoxAddress.Link link = BoxAddress.parseLink(data.toString());
        if (link == null) return false;
        if (link.setup) {
            showSetup();
            address.requestFocus();
            return true;
        }

        String saved = prefs.getString(KEY_BOX, null);
        String known = boxOrigin != null ? boxOrigin : saved;
        String target = link.origin != null ? link.origin : known;
        if (target == null) {
            // raum:// link before any box is set up: keep the room, ask for the box
            pendingPath = link.path;
            showSetup();
            showNote(getString(R.string.pending, BoxAddress.roomOf(link.path)));
            return true;
        }
        if (target.equals(boxOrigin)) {
            open(boxOrigin, link.path);
        } else if (target.equals(known)) {
            connect(target, link.path);
        } else {
            // A QR code for a box this phone does not use yet: never switch boxes silently.
            pendingPath = link.path;
            address.setText(target);
            showSetup();
            showNote(known == null
                    ? getString(R.string.pending, BoxAddress.roomOf(link.path))
                    : getString(R.string.other_box, target));
        }
        return true;
    }

    /** Check {@code <address>/api/health} off the main thread, then open the box. */
    private void connect(String input, String path) {
        final String origin;
        try {
            origin = BoxAddress.normalize(input);
        } catch (BoxAddress.Problem p) {
            showSetup();
            showProblem(p);
            return;
        }
        final int mine = ++attempt;
        pendingPath = path;
        busy(true);
        showStatus(getString(R.string.checking, origin), null, false);
        io.execute(() -> {
            BoxAddress.Problem problem = null;
            try {
                checkHealth(origin);
            } catch (BoxAddress.Problem p) {
                problem = p;
            }
            final BoxAddress.Problem result = problem;
            ui.post(() -> {
                if (isDestroyed() || mine != attempt) return;
                busy(false);
                if (result != null) {
                    showSetup();
                    showProblem(result);
                    return;
                }
                prefs.edit().putString(KEY_BOX, origin).apply();
                String p = pendingPath;
                pendingPath = null;
                open(origin, p);
            });
        });
    }

    private void open(String origin, String path) {
        boolean showing = origin.equals(boxOrigin) && BoxAddress.sameOrigin(origin, web.getUrl());
        boxOrigin = origin;
        address.setText(origin);
        status.setVisibility(View.GONE);
        note.setVisibility(View.GONE);
        if (showing && path == null) {
            // Same box again (after an error, or the address panel): reload the page it was on;
            // the web app rejoins its session from the box. Back, not Connect, keeps the page as is.
            showWeb();
            web.reload();
            return;
        }
        setup.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
        web.loadUrl(origin + (path == null ? "/" : path));
    }

    private static void checkHealth(String origin) throws BoxAddress.Problem {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(origin + "/api/health").openConnection();
            c.setConnectTimeout(TIMEOUT_MS);
            c.setReadTimeout(TIMEOUT_MS);
            c.setInstanceFollowRedirects(false);
            c.setUseCaches(false);
            c.setRequestProperty("Accept", "application/json");
            int code = c.getResponseCode();
            if (code != 200) throw new BoxAddress.Problem("not_raum", origin + " answered HTTP " + code);
            BoxAddress.checkHealth(read(c.getInputStream()));
        } catch (SSLException e) {
            String m = String.valueOf(e.getMessage());
            if (m.contains("packet header") || m.contains("plaintext") || m.contains("wrong version")) {
                throw new BoxAddress.Problem("not_raum", m + " (this port does not speak https)");
            }
            throw new BoxAddress.Problem("tls", m);
        } catch (SocketTimeoutException e) {
            throw new BoxAddress.Problem("unreachable", "no answer within " + TIMEOUT_MS / 1000 + " s");
        } catch (UnknownHostException | ConnectException e) {
            throw new BoxAddress.Problem("unreachable", String.valueOf(e.getMessage()));
        } catch (IOException e) {
            throw new BoxAddress.Problem("unreachable", e.toString());
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private static String read(InputStream in) throws IOException {
        try (InputStream is = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = is.read(buf)) != -1) {
                out.write(buf, 0, n);
                if (out.size() > BODY_LIMIT) throw new IOException("answer too large");
            }
            return new String(out.toByteArray(), StandardCharsets.UTF_8);
        }
    }

    // ------------------------------------------------------------------ WebView

    private void configureWebView() {
        WebView.setWebContentsDebuggingEnabled((getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true); // the web app remembers language and audio route
        s.setMediaPlaybackRequiresUserGesture(false); // voice output starts without a tap
        s.setAllowFileAccess(false);
        s.setGeolocationEnabled(false);
        s.setSupportMultipleWindows(false);
        s.setUserAgentString(s.getUserAgentString() + " RAUM-Android");
        web.setWebViewClient(new Client());
        web.setWebChromeClient(new Chrome());
    }

    private boolean onBox(String url) {
        return BoxAddress.sameOrigin(boxOrigin, url);
    }

    /** Screen stays on in a conversation (talk, table screen, listen-only), not on the other pages. */
    private void keepAwake(String url) {
        boolean on = setup.getVisibility() != View.VISIBLE && onBox(url) && BoxAddress.isConversation(url);
        if (on) getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    }

    private void print() {
        PrintManager pm = (PrintManager) getSystemService(PRINT_SERVICE);
        if (pm == null) return;
        String title = web.getTitle() == null || web.getTitle().isEmpty() ? "RAUM" : web.getTitle();
        pm.print(title, web.createPrintDocumentAdapter(title), new PrintAttributes.Builder().build());
    }

    private final class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri url = request.getUrl();
            String u = url.toString();
            String scheme = url.getScheme() == null ? "" : url.getScheme();
            if (PRINT_URL.equals(u)) {
                if (onBox(view.getUrl())) print();
                return true;
            }
            if (scheme.equals("about") || onBox(u)) return false;
            if (!request.isForMainFrame()) return true;
            if (scheme.equals("raum")) {
                handleLink(new Intent(Intent.ACTION_VIEW, url));
                return true;
            }
            // Anything else leaves the app (browser, mail), and only when the person tapped it.
            if (request.hasGesture() && (scheme.equals("https") || scheme.equals("mailto") || scheme.equals("tel"))) {
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, url));
                } catch (ActivityNotFoundException ignored) {
                    // nothing can open it
                }
            }
            return true;
        }

        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            keepAwake(url);
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            if (onBox(url)) view.evaluateJavascript(PRINT_SHIM, null);
        }

        @Override
        public void doUpdateVisitedHistory(WebView view, String url, boolean isReload) {
            keepAwake(url); // client-side route changes (join -> talk -> summary)
        }

        @Override
        public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
            handler.cancel(); // never proceed past a certificate error
            if (onBox(error.getUrl())) {
                showSetup();
                showProblem(new BoxAddress.Problem("tls", error.toString()));
            }
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (!request.isForMainFrame()) return;
            showSetup();
            showProblem(new BoxAddress.Problem("unreachable",
                    error.getDescription() + " (" + error.getErrorCode() + ")"));
        }

        @Override
        public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
            // The page's process died (or was reclaimed): start over; the session lives on the box.
            recreate();
            return true;
        }
    }

    private final class Chrome extends WebChromeClient {
        @Override
        public void onPermissionRequest(PermissionRequest request) {
            boolean audio = Arrays.asList(request.getResources()).contains(AUDIO);
            if (!audio || !onBox(request.getOrigin().toString())) {
                request.deny();
                return;
            }
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                request.grant(new String[] {AUDIO});
                return;
            }
            if (pendingMic != null) pendingMic.deny();
            pendingMic = request;
            requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, REQ_MIC);
        }

        @Override
        public void onPermissionRequestCanceled(PermissionRequest request) {
            if (request == pendingMic) pendingMic = null;
        }

        @Override
        public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback callback) {
            callback.invoke(origin, false, false);
        }

        /** {@code <input type=file>}: the glossary upload, and the document camera (capture). */
        @Override
        public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
            if (!onBox(view.getUrl())) return false;
            if (fileCallback != null) fileCallback.onReceiveValue(null);
            fileCallback = callback;
            cameraShot = null;
            Intent intent = params.isCaptureEnabled() && acceptsImages(params) ? cameraIntent() : null;
            if (intent == null) intent = params.createIntent();
            try {
                startActivityForResult(intent, REQ_FILE);
                return true;
            } catch (ActivityNotFoundException e) {
                fileCallback = null;
                cameraShot = null;
                return false;
            }
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        if (requestCode != REQ_MIC || pendingMic == null) return;
        PermissionRequest request = pendingMic;
        pendingMic = null;
        if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            request.grant(new String[] {AUDIO});
        } else {
            request.deny();
            Toast.makeText(this, R.string.mic_denied, Toast.LENGTH_LONG).show();
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_FILE || fileCallback == null) return;
        Uri[] result = null;
        if (resultCode == RESULT_OK) {
            result = cameraShot != null
                    ? new Uri[] {cameraShot}
                    : WebChromeClient.FileChooserParams.parseResult(resultCode, data);
        }
        fileCallback.onReceiveValue(result);
        fileCallback = null;
        cameraShot = null;
    }

    // ------------------------------------------------------------------ document camera

    private static boolean acceptsImages(WebChromeClient.FileChooserParams params) {
        String[] types = params.getAcceptTypes();
        if (types == null || types.length == 0) return true;
        for (String t : types) {
            if (t == null || t.isEmpty() || t.startsWith("image/") || t.equals("*/*")) return true;
        }
        return false;
    }

    /** The system camera app writes the photo into our cache (ShotProvider); no CAMERA permission. */
    private Intent cameraIntent() {
        clearShots();
        Uri uri = ShotProvider.newShot(this);
        if (uri == null) return null;
        Intent cam = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
        cam.putExtra(MediaStore.EXTRA_OUTPUT, uri);
        cam.setClipData(ClipData.newRawUri("", uri));
        cam.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_READ_URI_PERMISSION);
        if (cam.resolveActivity(getPackageManager()) == null) return null;
        cameraShot = uri;
        return cam;
    }

    /** Earlier photos: already sent to the box (or abandoned). */
    private void clearShots() {
        ShotProvider.clear(this);
    }

    // ------------------------------------------------------------------ the address panel

    private void showSetup() {
        web.setVisibility(View.INVISIBLE);
        setup.setVisibility(View.VISIBLE);
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    }

    private void showWeb() {
        setup.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
        keepAwake(web.getUrl());
    }

    private void busy(boolean on) {
        connect.setEnabled(!on);
        address.setEnabled(!on);
    }

    private void showNote(String text) {
        note.setText(text);
        note.setVisibility(View.VISIBLE);
    }

    private void showStatus(String text, String detail, boolean bad) {
        status.setVisibility(View.VISIBLE);
        status.setBackgroundResource(bad ? R.drawable.status_bad : R.drawable.status);
        statusText.setText(text);
        statusText.setTextColor(getColor(bad ? R.color.bad : R.color.ink));
        statusDetail.setText(detail == null ? "" : detail);
        statusDetail.setVisibility(detail == null || detail.isEmpty() ? View.GONE : View.VISIBLE);
    }

    private void showProblem(BoxAddress.Problem p) {
        showStatus(getString(messageFor(p.code)), p.getMessage(), true);
    }

    private static int messageFor(String code) {
        switch (code) {
            case "insecure":
                return R.string.problem_insecure;
            case "tls":
                return R.string.problem_tls;
            case "unreachable":
                return R.string.problem_unreachable;
            case "not_raum":
                return R.string.problem_not_raum;
            default:
                return R.string.problem_address;
        }
    }
}
