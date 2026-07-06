// Root build script — this is a SOURCE SKELETON (Milestone 2, W3). It has never been
// built: this box has JDK 17 only, no Android SDK / Gradle wrapper jar / Kotlin
// toolchain. See README.md before assuming any of this resolves or compiles.
plugins {
    id("com.android.application") version "8.5.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.24" apply false
    id("org.jetbrains.kotlin.plugin.serialization") version "1.9.24" apply false
    id("com.google.devtools.ksp") version "1.9.24-1.0.20" apply false
}
