plugins {
    id("com.android.application")
}

// Host for https://<host>/j/<room> links (the QR codes). "*" matches any host, which Android 11 and
// older offer in a chooser; Android 12+ only opens a fixed host, after Settings > Apps > RAUM >
// Open by default > Add link. Build with -PraumLinkHost=192.168.1.50 for that (android/README.md).
val raumLinkHost: String = (findProperty("raumLinkHost") as String?)?.trim().takeUnless { it.isNullOrEmpty() } ?: "*"
require(raumLinkHost == "*" || Regex("^[A-Za-z0-9.-]+$").matches(raumLinkHost)) {
    "raumLinkHost must be * or a host name / IPv4 address, got '$raumLinkHost'"
}

android {
    namespace = "de.raum.android"
    compileSdk = 35
    buildToolsVersion = "35.0.0"

    defaultConfig {
        applicationId = "de.raum.android"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
        manifestPlaceholders["raumLinkHost"] = raumLinkHost
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

// No runtime dependencies: the platform WebView, nothing else.
dependencies {
    testImplementation("junit:junit:4.13.2")
    // Real org.json for JVM unit tests (android.jar only has stubs).
    testImplementation("org.json:json:20240303")
}
