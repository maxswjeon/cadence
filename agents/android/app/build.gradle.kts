plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.serialization")
    id("com.google.devtools.ksp")
}

android {
    namespace = "com.cadence.agent"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.cadence.agent"
        minSdk = 26 // BluetoothLeScanner + modern foreground-service types need 26+.
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0-skeleton"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    // TODO(device): once `agents/core` (W2) publishes a `#[no_mangle] extern "C"` JNI
    // export layer, add a Gradle task that runs `cargo ndk -o app/src/main/jniLibs build
    // --release` (per-ABI: arm64-v8a, armeabi-v7a, x86_64) and wire it as a `preBuild`
    // dependency, OR drop prebuilt `.so`s directly under `app/src/main/jniLibs/<abi>/`.
    // Not configured here — see core/CoreBridge.kt for the honest current status.
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.lifecycle:lifecycle-service:2.8.4")

    // Device-side WAL (durability net ahead of / independent from agents/core's own
    // WAL once CoreBridge is linked — see wal/EventEntity.kt).
    implementation("androidx.room:room-runtime:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")
    ksp("androidx.room:room-compiler:2.6.1")

    // FusedLocationProviderClient backing LocationBleCollector's place-based prior.
    implementation("com.google.android.gms:play-services-location:21.3.0")

    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    // JsonObject for EventEnvelope.structured (contract/event-envelope.schema.json).
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.1")

    testImplementation("junit:junit:4.13.2")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
}
