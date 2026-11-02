package de.raum.android;

import java.io.UnsupportedEncodingException;
import java.net.URI;
import java.net.URISyntaxException;
import java.net.URLDecoder;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.regex.Pattern;

import org.json.JSONException;
import org.json.JSONObject;

/**
 * Box addresses, links and the health answer. Plain Java ({@link URI}, no android.net.Uri), so the
 * rules are unit-tested on the JVM; the same rules as desktop/src-tauri/src/target.rs.
 */
final class BoxAddress {
    private BoxAddress() {}

    private static final Pattern ROOM = Pattern.compile("[A-Za-z0-9_-]{1,64}");
    private static final Pattern LANG = Pattern.compile("[a-z]{2,3}"); // as raum-core accepts it
    private static final Pattern CONVERSATION = Pattern.compile("^/(r|t|l)/[^/]+.*");

    /** What went wrong: a code the activity maps to a sentence, plus a detail line for ops. */
    static final class Problem extends Exception {
        final String code;

        Problem(String code, String detail) {
            super(detail);
            this.code = code;
        }
    }

    /** A link the app was opened with. {@code origin == null}: a raum:// link for the configured box. */
    static final class Link {
        final String origin;
        final String path;
        final boolean setup;

        private Link(String origin, String path, boolean setup) {
            this.origin = origin;
            this.path = path;
            this.setup = setup;
        }
    }

    /**
     * {@code 192.168.1.50:5180} or {@code https://192.168.1.50:5180/j/x?role=b} to
     * {@code https://192.168.1.50:5180}. HTTPS only: the microphone needs a secure context.
     */
    static String normalize(String input) throws Problem {
        String s = input == null ? "" : input.trim();
        if (s.isEmpty()) throw new Problem("address", "empty address");
        if (!s.contains("://")) s = "https://" + s;
        URI u = parse(s);
        String scheme = lower(u.getScheme());
        if ("http".equals(scheme)) throw new Problem("insecure", "plain http: the microphone needs https");
        if (!"https".equals(scheme)) throw new Problem("address", "unsupported scheme " + scheme + ":");
        if (u.getRawUserInfo() != null) {
            throw new Problem("address", "user names and passwords do not belong in the address");
        }
        if (u.getHost() == null || u.getHost().isEmpty()) throw new Problem("address", "no host in the address");
        return originOf(s);
    }

    /** Scheme, host and port of any URL; default ports dropped ({@code https://h:443/x} to {@code https://h}). */
    static String originOf(String url) throws Problem {
        URI u = parse(url);
        String scheme = lower(u.getScheme());
        String host = u.getHost();
        if (scheme == null || host == null || host.isEmpty()) throw new Problem("address", "no origin in " + url);
        int port = u.getPort();
        if (("https".equals(scheme) && port == 443) || ("http".equals(scheme) && port == 80)) port = -1;
        return scheme + "://" + host.toLowerCase(Locale.ROOT) + (port == -1 ? "" : ":" + port);
    }

    static boolean sameOrigin(String origin, String url) {
        if (origin == null || url == null) return false;
        try {
            return originOf(url).equals(origin);
        } catch (Problem e) {
            return false;
        }
    }

    /** Talk, table screen, listen-only: the pages that keep the screen on. */
    static boolean isConversation(String url) {
        try {
            String path = parse(url).getRawPath();
            return path != null && CONVERSATION.matcher(path).matches();
        } catch (Problem e) {
            return false;
        }
    }

    /**
     * Links that open the app. Only known routes and parameters pass.
     * <ul>
     *   <li>{@code raum://j/<room>?role=a|b&lang=xx}, {@code raum://join?room=..}: /j/ on the configured box</li>
     *   <li>{@code raum://l/<room>}, {@code raum://t/<room>?theme=dark|light}, {@code raum://open}</li>
     *   <li>{@code raum://setup}: the box-address panel (launcher shortcut)</li>
     *   <li>{@code https://<box>/j/<room>?role=..}: the QR codes; the origin comes with the link</li>
     * </ul>
     *
     * @return null for anything else
     */
    static Link parseLink(String raw) {
        if (raw == null) return null;
        if (raw.equals("raum:") || raw.equals("raum://")) return new Link(null, "/", false);
        URI u;
        try {
            u = parse(raw);
        } catch (Problem e) {
            return null;
        }
        String scheme = lower(u.getScheme());
        List<String> segments = segments(u.getRawPath());
        String query = u.getRawQuery();

        if ("raum".equals(scheme)) {
            String host = u.getHost() == null ? "" : u.getHost();
            switch (host) {
                case "setup":
                    return segments.isEmpty() ? new Link(null, null, true) : null;
                case "open":
                case "":
                    return segments.isEmpty() ? new Link(null, "/", false) : null;
                case "join": {
                    String room = param(query, "room");
                    return segments.isEmpty() && room != null ? route(null, "j", room, query) : null;
                }
                case "j":
                case "l":
                case "t":
                    return segments.size() == 1 ? route(null, host, segments.get(0), query) : null;
                default:
                    return null;
            }
        }
        if ("https".equals(scheme) && segments.size() == 2 && segments.get(0).equals("j")) {
            try {
                return route(originOf(raw), "j", segments.get(1), query);
            } catch (Problem e) {
                return null;
            }
        }
        return null;
    }

    /** "/j/sz-1234?role=b" to "sz-1234". */
    static String roomOf(String path) {
        if (path == null) return "";
        String p = path.split("\\?", 2)[0];
        return p.substring(p.lastIndexOf('/') + 1);
    }

    /** Accept only raum-core's own answer to /api/health ({@code "service": "raum-core"}). */
    static void checkHealth(String body) throws Problem {
        String service;
        try {
            service = new JSONObject(body).optString("service", "");
        } catch (JSONException e) {
            throw new Problem("not_raum", "the answer to /api/health is not JSON");
        }
        if (!"raum-core".equals(service)) {
            throw new Problem("not_raum", "/api/health does not say service: raum-core");
        }
    }

    private static Link route(String origin, String route, String room, String query) {
        if (!ROOM.matcher(room).matches()) return null;
        StringBuilder qs = new StringBuilder();
        if (route.equals("j")) {
            String role = param(query, "role");
            if ("a".equals(role) || "b".equals(role)) append(qs, "role", role);
            String lang = param(query, "lang");
            if (lang != null && LANG.matcher(lang).matches()) append(qs, "lang", lang);
        } else if (route.equals("t")) {
            String theme = param(query, "theme");
            if ("dark".equals(theme) || "light".equals(theme)) append(qs, "theme", theme);
        }
        String path = "/" + route + "/" + room + (qs.length() > 0 ? "?" + qs : "");
        return new Link(origin, path, false);
    }

    private static void append(StringBuilder qs, String key, String value) {
        if (qs.length() > 0) qs.append('&');
        qs.append(key).append('=').append(value); // values are validated, nothing to encode
    }

    private static String param(String rawQuery, String key) {
        if (rawQuery == null) return null;
        for (String pair : rawQuery.split("&")) {
            int eq = pair.indexOf('=');
            String k = eq < 0 ? pair : pair.substring(0, eq);
            if (!k.equals(key)) continue;
            try {
                return URLDecoder.decode(eq < 0 ? "" : pair.substring(eq + 1), "UTF-8");
            } catch (UnsupportedEncodingException | IllegalArgumentException e) {
                return null;
            }
        }
        return null;
    }

    private static List<String> segments(String rawPath) {
        List<String> out = new ArrayList<>();
        if (rawPath == null) return out;
        for (String s : rawPath.split("/")) {
            if (!s.isEmpty()) out.add(s);
        }
        return out;
    }

    private static URI parse(String s) throws Problem {
        try {
            return new URI(s);
        } catch (URISyntaxException e) {
            throw new Problem("address", e.getMessage());
        }
    }

    private static String lower(String s) {
        return s == null ? null : s.toLowerCase(Locale.ROOT);
    }
}
