# Releasing Micophone

A release is a git tag. Pushing `v2.0.1` makes
[`release.yml`](../.github/workflows/release.yml) build the Windows app and the Android app, sign them, and
publish them on GitHub Releases with generated notes.

```powershell
# 1. Bump versions (keep them in step):
#    pc/micophone_gui.py            VERSION
#    pc/version_info.txt            filevers / prodvers / FileVersion / ProductVersion
#    android/app/build.gradle.kts   versionName, and versionCode +1
# 2. Commit, then:
git tag v2.0.1
git push origin main v2.0.1
```

## One-time setup

### Android signing key (GitHub secrets)

The same key must sign every release, or phones refuse the update. In *Settings › Secrets and variables ›
Actions* add:

| Secret | Value |
|---|---|
| `ANDROID_KEYSTORE_BASE64` | `[Convert]::ToBase64String([IO.File]::ReadAllBytes("android\phonemic-release.jks"))` |
| `ANDROID_KEYSTORE_PASSWORD` | the password in `android/keystore.properties` (`storePassword`, same as `keyPassword`) |

The key alias is `phonemic`. Without these secrets the release workflow stops with an error, because an unsigned APK can't be installed.
**Never commit `keystore.properties` or any `.jks` file** (both are in `.gitignore`).

### Windows code signing with SignPath Foundation (free for open source)

1. **Apply** at <https://signpath.org/apply> for the repository `https://github.com/farzonline/micophone`
   (public, MIT license, with the *Code signing policy* section already in the README). Approval takes
   from days to a few weeks.
2. Once approved, in SignPath create the project with **slug `micophone`**, paste
   [`.signpath/artifact-configuration.xml`](../.signpath/artifact-configuration.xml) as its artifact
   configuration, and create a signing policy with **slug `release-signing`**. Install the SignPath GitHub app
   and add the repository as a trusted build system.
3. In GitHub *Settings › Secrets and variables › Actions*:
   - secret `SIGNPATH_API_TOKEN` (a SignPath API token for a CI user);
   - variable `SIGNPATH_ORGANIZATION_ID` (shown in SignPath).
4. Push a tag. Until step 3 is done the signing step is skipped and an unsigned `Micophone.exe` is
   published. Approving the signing request in SignPath is a manual click for each release, by design.

SmartScreen warnings fade gradually as more people download the signed file; a brand-new signed binary can still
show one for a short while.

### Google Play

Upload `Micophone-play-store.aab` from the release. Checklist: package `com.farzonline.micophone` (permanent),
the [privacy policy](../PRIVACY.md) URL, the *microphone* foreground-service declaration with a short video, a
data safety form (microphone audio goes only to the user's own PC), screenshots, and a 1024×500 feature graphic.
The store icon is `android/store/icon-512.png`.
