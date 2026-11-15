# OpenMettle palette

The family Open WebUI uses OpenMettle's palette everywhere: warm ivory and graphite neutrals with one vermilion accent. The chat area, welcome and icons come from the fork (see [OpenMettle theme](../../../../docs/ASSISTANT.md#openmettle-theme)); this folder carries the rest of the app.

| File | Use |
| --- | --- |
| `custom.css` | Open WebUI's grey scale retinted (ivory at the light end, graphite at the dark end), the blue and sky accents mapped to vermilion, the text selection colour and the sign-in backdrop |

## How it is applied

At startup, Open WebUI empties its static folder and copies `/app/build/static` into it. The assistant component (`../webui.yaml`) mounts `custom.css` over `/app/build/static/custom.css` from the `webui-theme` ConfigMap. The ConfigMap name carries a hash of its contents, so changing the file rolls Open WebUI on the next deployment.

No icons are mounted. With `OPENMETTLE_BRANDING=true` (in `../webui.yaml`) the fork puts Mettle's head in place of Open WebUI's stock favicon, splash and install icons, and the model avatar falls back to the same icon. An icon mounted here would win over the fork's, because the switch replaces only Open WebUI's own files.

Each user still picks Light, Dark or OLED in their own settings. OLED keeps true-black backgrounds, because Open WebUI sets those two shades as inline styles.

## Changing it

- **Colours.** Edit `custom.css`. Measure text against its background before committing: 4.5:1 for text, 3:1 for outlines, borders and icons (`docs/ASSISTANT.md` lists the pairs measured). Light mode darkens greys 400–700 for secondary text; the `.dark` block keeps upstream lightness for dark mode.
- **The icons and the welcome art.** They live in the fork (`static/static/openmettle/`), rendered by its `scripts/openmettle/render_assets.py` from the OpenMettle brand originals.
- **A new file.** Add it to the `webui-theme` generator in `../kustomization.yaml` and to the mounts in `../webui.yaml`. Also add it to `infrastructure/packages/bootstrap/source.files` and `bridge-runtime.files`.

The title reads "Family AI (OpenMettle)": the fork appends the product name to any custom `WEBUI_NAME`, and the branding switch makes that product OpenMettle instead of Open WebUI.

## Licence

Open WebUI's licence forbids replacing its branding, with one exception that applies here: deployments with at most 50 end users within any rolling 30 days. If this deployment ever grows past that, set `OPENMETTLE_BRANDING` to `'false'` in `../webui.yaml`; Open WebUI's name and icons come back. The palette is colour only and can stay.
