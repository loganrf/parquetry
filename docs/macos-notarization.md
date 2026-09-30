# macOS signing and notarization

The release workflow signs the macOS app with a Developer ID certificate and has
Apple notarize it, so the downloaded app opens with a double-click instead of a
Gatekeeper warning. This needs a one-time setup with a paid Apple Developer
Program membership: a certificate, an API key for the notary service and five
GitHub secrets. Until the secrets are set, tagged releases fail on the macOS
jobs, and manual runs of the workflow build unsigned macOS bundles.

## What the workflow does

For each macOS architecture, `.github/workflows/release.yml`:

1. imports the certificate into a temporary keychain,
2. builds `Parquetry.app` with PyInstaller, which signs every binary with the
   hardened runtime and the entitlements in `packaging/macos/entitlements.plist`,
3. runs the smoke test against the signed app,
4. notarizes the app and staples the ticket to it
   (`packaging/macos/notarize.py`),
5. builds the disk image and signs it (`packaging/package.py`),
6. notarizes the disk image and staples the ticket to it.

The app is notarized on its own as well as inside the disk image, so it keeps
its ticket when it is copied out of the disk image and opens without an
internet connection.

## One-time setup (needs the Apple Developer account)

### 1. Create a Developer ID Application certificate

Creating a Developer ID certificate needs the Account Holder role. On an
individual membership, that is you. The certificate is valid for five years.

**On a Mac (Keychain Access):**

1. Open *Keychain Access → Certificate Assistant → Request a Certificate From a
   Certificate Authority*. Enter your email and name, choose *Saved to disk*
   and save the `.certSigningRequest` file.
2. Go to [Certificates, Identifiers & Profiles](https://developer.apple.com/account/resources/certificates/list),
   click **+**, choose **Developer ID Application** (not *Developer ID
   Installer*, *Apple Distribution* or *Mac App Distribution*), keep the
   **G2 Sub-CA** profile, upload the request and download the `.cer` file.
3. Double-click the `.cer` file to add it to the *login* keychain.
4. In Keychain Access, open *My Certificates*, right-click
   **Developer ID Application: Your Name (TEAMID)** and choose *Export*. Save it
   as `developer_id.p12` with a strong password. It must be under *My
   Certificates* (with its private key), otherwise the `.p12` cannot sign.

**Without a Mac (OpenSSL):**

```bash
openssl genrsa -out developer_id.key 2048
openssl req -new -key developer_id.key -out developer_id.csr \
    -subj "/emailAddress=you@example.com/CN=Your Name/C=US"
# Upload developer_id.csr as in step 2 above and download developerID_application.cer, then:
openssl x509 -inform DER -in developerID_application.cer -out developer_id.pem
openssl pkcs12 -export -legacy -inkey developer_id.key -in developer_id.pem -out developer_id.p12
```

`-legacy` makes a `.p12` that the macOS `security` tool can import. Leave it
out if you use OpenSSL 1.1.

Keep `developer_id.p12` (or `developer_id.key`) somewhere safe and offline.
Anyone who has it can sign software in your name.

### 2. Create an App Store Connect API key for the notary service

1. In [App Store Connect](https://appstoreconnect.apple.com/access/integrations/api),
   go to *Users and Access → Integrations → App Store Connect API*. The first
   time, the Account Holder has to request access.
2. Under **Team Keys**, click **+** (Generate API Key), give it a name such as
   `parquetry-notarization` and the **Developer** access role.
3. Download the `AuthKey_<KEYID>.p8` file. Apple lets you download it
   **only once**.
4. Write down the **Key ID** (in the key's row) and the **Issuer ID** (shown
   above the table).

An **Individual Key** (same page, *Individual Keys* tab) also works. It has no
issuer ID, so leave `APPLE_API_ISSUER_ID` unset in the next step.

### 3. Add the GitHub secrets

Add these under *Settings → Secrets and variables → Actions → New repository
secret* in the GitHub repository:

| Secret | Value |
|---|---|
| `MACOS_CERTIFICATE_P12` | `developer_id.p12`, base64 encoded on one line |
| `MACOS_CERTIFICATE_PASSWORD` | The password of `developer_id.p12` |
| `APPLE_API_KEY_P8` | The full text of `AuthKey_<KEYID>.p8`, including the `BEGIN`/`END` lines |
| `APPLE_API_KEY_ID` | The Key ID, for example `2X9R4HXF34` |
| `APPLE_API_ISSUER_ID` | The Issuer ID, a UUID. Team keys only: do not add it for an individual key |

With the [GitHub CLI](https://cli.github.com/), from the folder with the files:

```bash
base64 -i developer_id.p12 | tr -d '\n' | gh secret set MACOS_CERTIFICATE_P12 -R loganrf/parquetry
gh secret set MACOS_CERTIFICATE_PASSWORD -R loganrf/parquetry          # prompts for the value
gh secret set APPLE_API_KEY_P8 -R loganrf/parquetry < AuthKey_<KEYID>.p8
gh secret set APPLE_API_KEY_ID -R loganrf/parquetry --body "<KEYID>"
gh secret set APPLE_API_ISSUER_ID -R loganrf/parquetry --body "<ISSUER-ID>"
```

The release workflow only runs for tags and manual runs, so pull requests
(including ones from forks) never see these secrets.

### 4. Test it without publishing a release

1. In the repository, open *Actions → Release → Run workflow*. A manual run
   builds, signs and notarizes everything but does not publish a release.
2. When it has finished, download `bundle-macos-arm64` (or `-x86_64`) from the
   run's summary page on a Mac and unzip it.
3. Check the disk image, then install the app and check it too:

   ```bash
   xcrun stapler validate parquetry-*-macos-*.dmg
   spctl -a -vv -t open --context context:primary-signature parquetry-*-macos-*.dmg
   # after dragging Parquetry into Applications:
   spctl -a -vv /Applications/Parquetry.app
   ```

   `spctl` should print `accepted` and `source=Notarized Developer ID`.

After that, publish releases as before: update `__version__` and push a tag.

## Keeping it working

- **Membership:** notarizing needs an active Apple Developer Program membership.
  Releases that are already notarized keep working if it lapses.
- **Certificate expiry (5 years):** create a new Developer ID Application
  certificate and replace `MACOS_CERTIFICATE_P12` and
  `MACOS_CERTIFICATE_PASSWORD`. Releases signed with the old one stay valid
  because every signature has a secure timestamp. Do not revoke the old
  certificate unless its private key has leaked: revoking it can make
  Gatekeeper reject releases that were signed with it.
- **API key:** API keys do not expire. If one leaks, revoke it in App Store
  Connect, create a new one and update the `APPLE_API_*` secrets.

## Troubleshooting

The notarization steps print Apple's notarization log, which names every file
Apple rejected and why.

| Problem | Cause and fix |
|---|---|
| *does not contain a Developer ID Application certificate* | The `.p12` has the wrong certificate type or no private key. Export it again from *My Certificates*. |
| `security import` fails (*MAC verification failed*) | Wrong password, or a `.p12` made by OpenSSL 3 without `-legacy`. |
| `notarytool` reports *401* / *Unauthorized* | Wrong key ID, or the issuer ID does not fit the key type (team keys need it, individual keys must not have it). |
| Notarization status *Invalid* | See the log. Typical causes are a binary that is not signed or not signed with the hardened runtime. |
| The signed app crashes but the unsigned one does not | The hardened runtime blocks something the code needs. Add the matching entitlement to `packaging/macos/entitlements.plist`. |

## Signing and notarizing on your own Mac

You can do the same steps locally with the certificate in your login keychain:

```bash
# once: store the notary credentials in the keychain (use --issuer only for team keys)
xcrun notarytool store-credentials parquetry --key AuthKey_<KEYID>.p8 --key-id <KEYID> --issuer <ISSUER-ID>

export MACOS_CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export APPLE_NOTARY_PROFILE=parquetry
pyinstaller packaging/parquetry.spec --noconfirm
python packaging/smoke_test.py dist
python packaging/macos/notarize.py dist/Parquetry.app
python packaging/package.py dist artifacts
python packaging/macos/notarize.py artifacts/*.dmg
```
