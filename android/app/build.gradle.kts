plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.mrkaan840.gimbalcam"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.mrkaan840.gimbalcam"
        minSdk = 29
        targetSdk = 35
        versionCode = 9
        versionName = "1.6.1"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}
