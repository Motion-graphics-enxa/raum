package de.raum.android;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.junit.Test;

/** JVM unit tests (gradle testDebugUnitTest); the same cases as desktop/src-tauri/src/target.rs. */
public class BoxAddressTest {

    private static String problem(String input) {
        try {
            BoxAddress.normalize(input);
        } catch (BoxAddress.Problem p) {
            return p.code;
        }
        fail(input + " should be rejected");
        return null;
    }

    @Test
    public void normalizeAddsHttpsAndKeepsOnlyTheOrigin() throws Exception {
        assertEquals("https://192.168.1.50:5180", BoxAddress.normalize("192.168.1.50:5180"));
        assertEquals("https://192.168.1.50:5180", BoxAddress.normalize(" https://192.168.1.50:5180/j/sz-1?role=b#x "));
        assertEquals("https://box.example.ts.net", BoxAddress.normalize("https://box.example.ts.net:443"));
        assertEquals("https://raum.local", BoxAddress.normalize("HTTPS://Raum.Local"));
        assertEquals("https://[fd00::5]:5180", BoxAddress.normalize("[fd00::5]:5180"));
    }

    @Test
    public void normalizeRejectsPlainHttpAndJunk() {
        assertEquals("insecure", problem("http://192.168.1.50:5180"));
        assertEquals("insecure", problem("http://localhost:5180"));
        for (String bad : new String[] {"", "   ", "ftp://box", "https://", "https://user:pw@box", "javascript:alert(1)"}) {
            problem(bad);
        }
    }

    @Test
    public void sameOriginComparesSchemeHostPort() throws Exception {
        String box = BoxAddress.normalize("192.168.1.50:5180");
        assertTrue(BoxAddress.sameOrigin(box, "https://192.168.1.50:5180/r/sz-1"));
        assertTrue(BoxAddress.sameOrigin(box, "https://192.168.1.50:5180/")); // PermissionRequest.getOrigin()
        assertFalse(BoxAddress.sameOrigin(box, "https://192.168.1.50/r/sz-1"));
        assertFalse(BoxAddress.sameOrigin(box, "http://192.168.1.50:5180/"));
        assertFalse(BoxAddress.sameOrigin(box, "https://192.168.1.51:5180/"));
        assertFalse(BoxAddress.sameOrigin(box, null));
        assertFalse(BoxAddress.sameOrigin(null, "https://192.168.1.50:5180/"));
        assertTrue(BoxAddress.sameOrigin(BoxAddress.normalize("box.example"), "https://box.example:443/x"));
    }

    @Test
    public void conversationPagesKeepTheScreenOn() {
        assertTrue(BoxAddress.isConversation("https://b/r/sz-1"));
        assertTrue(BoxAddress.isConversation("https://b/r/sz-1/summary"));
        assertTrue(BoxAddress.isConversation("https://b/t/sz-1?theme=dark"));
        assertTrue(BoxAddress.isConversation("https://b/l/sz-1"));
        assertFalse(BoxAddress.isConversation("https://b/j/sz-1?role=b"));
        assertFalse(BoxAddress.isConversation("https://b/"));
        assertFalse(BoxAddress.isConversation("https://b/admin"));
    }

    private static String path(String link) {
        BoxAddress.Link l = BoxAddress.parseLink(link);
        return l == null ? null : l.path;
    }

    @Test
    public void raumLinksMapToBoxPaths() {
        assertEquals("/j/sz-1234?role=b&lang=ar", path("raum://j/sz-1234?role=b&lang=ar"));
        assertEquals("/j/sz-1234", path("raum://j/sz-1234/"));
        assertEquals("/j/kz-77?role=a", path("raum://join?room=kz-77&role=a"));
        assertEquals("/l/sz-1234", path("raum://l/sz-1234"));
        assertEquals("/t/sz-1234?theme=dark", path("raum://t/sz-1234?theme=dark"));
        assertEquals("/", path("raum://open"));
        assertEquals("/", path("raum://"));
        assertNull(BoxAddress.parseLink("raum://j/sz-1").origin);
        assertTrue(BoxAddress.parseLink("raum://setup").setup);
    }

    @Test
    public void qrLinksCarryTheirBox() {
        BoxAddress.Link l = BoxAddress.parseLink("https://192.168.1.50:5180/j/sz-1?role=b&lang=tr");
        assertEquals("https://192.168.1.50:5180", l.origin);
        assertEquals("/j/sz-1?role=b&lang=tr", l.path);
        assertEquals("https://192.168.1.50", BoxAddress.parseLink("https://192.168.1.50:443/j/sz-1").origin);
        assertNull(BoxAddress.parseLink("https://192.168.1.50/admin"));
        assertNull(BoxAddress.parseLink("http://192.168.1.50/j/sz-1"));
    }

    @Test
    public void linksDropUnknownParametersAndRoutes() {
        assertEquals("/j/sz-1", path("raum://j/sz-1?role=admin&lang=EN&box=https://evil"));
        assertEquals("/j/sz-1", path("raum://j/sz-1?lang=de-DE"));
        assertNull(path("raum://admin"));
        assertNull(path("raum://r/sz-1"));
        assertNull(path("raum://j/..%2Fadmin"));
        assertNull(path("raum://j/a/b"));
        assertNull(path("raum://join"));
        assertNull(path("mailto:x@example.com"));
        StringBuilder longRoom = new StringBuilder();
        for (int i = 0; i < 65; i++) longRoom.append('x');
        assertNull(path("raum://j/" + longRoom));
    }

    @Test
    public void roomOfPath() {
        assertEquals("sz-1234", BoxAddress.roomOf("/j/sz-1234?role=b"));
        assertEquals("sz-1234", BoxAddress.roomOf("/l/sz-1234"));
    }

    @Test
    public void healthMustComeFromRaumCore() throws Exception {
        BoxAddress.checkHealth("{\"ok\":true,\"service\":\"raum-core\",\"profile\":\"spark\"}");
        for (String body : new String[] {"{\"ok\":true,\"service\":\"something-else\"}", "<html>router login</html>", ""}) {
            try {
                BoxAddress.checkHealth(body);
                fail(body + " should be rejected");
            } catch (BoxAddress.Problem p) {
                assertEquals("not_raum", p.code);
            }
        }
    }
}
