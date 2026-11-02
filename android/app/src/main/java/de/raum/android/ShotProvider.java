package de.raum.android;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;
import java.util.regex.Pattern;

/**
 * The document camera's photo, shared with exactly two parties: the system camera app (writes it,
 * via a one-off URI grant) and this app's WebView (reads it for the upload to the box). Serves only
 * {@code cache/doccam/page-<millis>.jpg}; not exported. Small stand-in for androidx FileProvider so
 * the app needs no AndroidX at all.
 */
public final class ShotProvider extends ContentProvider {
    private static final String DIR = "doccam";
    private static final Pattern NAME = Pattern.compile("page-\\d{1,19}\\.jpg");

    static String authority(Context context) {
        return context.getPackageName() + ".files";
    }

    /** A new, empty photo file and its content:// URI. */
    static Uri newShot(Context context) {
        File dir = new File(context.getCacheDir(), DIR);
        if (!dir.isDirectory() && !dir.mkdirs()) return null;
        String name = "page-" + System.currentTimeMillis() + ".jpg";
        return new Uri.Builder().scheme("content").authority(authority(context)).appendPath(name).build();
    }

    /** Remove earlier photos (sent to the box already, or abandoned). */
    static void clear(Context context) {
        File[] files = new File(context.getCacheDir(), DIR).listFiles();
        if (files == null) return;
        for (File f : files) {
            //noinspection ResultOfMethodCallIgnored
            f.delete();
        }
    }

    private File file(Uri uri) throws FileNotFoundException {
        Context context = getContext();
        String name = uri.getLastPathSegment();
        if (context == null || name == null || uri.getPathSegments().size() != 1 || !NAME.matcher(name).matches()) {
            throw new FileNotFoundException("no such photo: " + uri);
        }
        return new File(new File(context.getCacheDir(), DIR), name);
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        return ParcelFileDescriptor.open(file(uri), ParcelFileDescriptor.parseMode(mode));
    }

    @Override
    public String getType(Uri uri) {
        return "image/jpeg";
    }

    /** Name and size, as the WebView asks for them when it turns the photo into a File. */
    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) {
        File f;
        try {
            f = file(uri);
        } catch (FileNotFoundException e) {
            return null;
        }
        String[] cols = projection != null ? projection : new String[] {OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE};
        Object[] row = new Object[cols.length];
        for (int i = 0; i < cols.length; i++) {
            if (OpenableColumns.DISPLAY_NAME.equals(cols[i])) row[i] = f.getName();
            else if (OpenableColumns.SIZE.equals(cols[i])) row[i] = f.length();
        }
        MatrixCursor cursor = new MatrixCursor(cols, 1);
        cursor.addRow(row);
        return cursor;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        throw new UnsupportedOperationException("read-only");
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("read-only");
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("read-only");
    }
}
