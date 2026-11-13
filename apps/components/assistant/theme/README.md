# Family AI theme

The Open WebUI theme for the family assistant: a deep-green palette, a stylesheet, an app manifest and a set of icons.

| File | Source or generated | Use |
| --- | --- | --- |
| `logo.svg` | source | The mark: a sprout growing from a speech bubble, on a deep-green tile |
| `custom.css` | source | Greys tinted deep green, with upstream lightness kept; blue and sky accents mapped to greens |
| `site.webmanifest` | source | Installed-app name ("Family AI") and colours |
| `favicon.svg` | generated | A copy of `logo.svg` |
| `favicon.ico`, `favicon*.png`, `apple-touch-icon.png`, `web-app-manifest-*.png`, `logo.png`, `splash*.png` | generated | Rendered from `logo.svg` at Open WebUI's own sizes |

## How it is applied

At startup, Open WebUI empties its static folder and copies `/app/build/static` into it. The assistant component (`../webui.yaml`) mounts each file here over the same name in `/app/build/static`, from the `webui-theme` ConfigMap. No image or fork change is involved. The ConfigMap name carries a hash of its contents, so changing a file here rolls Open WebUI on the next deployment.

Each user still picks Light, Dark or OLED in their own settings, and this theme tints all three. OLED keeps true-black backgrounds, because Open WebUI sets those two shades as inline styles.

## Changing it

- **Colours.** Edit `custom.css`. Keep each grey at upstream lightness so text contrast is unchanged; only the tint (chroma 0.006–0.024, hue 160) differs from upstream.
- **The mark.** Edit `logo.svg`, then run `./render-assets.sh` and commit the regenerated files. The script needs podman and network access. It renders in the pinned Arch base image (`SUNSHINE_ARCH_BASE_IMAGE` in `versions.lock`) with librsvg and ImageMagick from the signed Arch repositories.
- **A new file.** Add it to the `webui-theme` generator in `../kustomization.yaml` and to the mounts in `../webui.yaml`. Also add it to `infrastructure/packages/bootstrap/source.files` and `bridge-runtime.files`.

The title reads "Family AI (Open WebUI)", because Open WebUI appends that suffix to any custom `WEBUI_NAME`.

## Licence

Open WebUI's licence forbids replacing its branding, with one exception that applies here: deployments with at most 50 end users within any rolling 30 days. If this deployment ever grows past that, restore the upstream assets by removing the `webui-theme` mounts. Alternatively, keep the Open WebUI logo and theme the colours only.
