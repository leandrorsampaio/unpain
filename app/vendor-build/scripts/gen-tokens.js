// Bundled by build-theme.mjs (esbuild resolves the extensionless imports inside this package).
import { themeFromSourceColor, argbFromHex, hexFromArgb } from '@material/material-color-utilities';

export function generate(seed) {
  const theme = themeFromSourceColor(argbFromHex(seed));
  const tokens = scheme => Object.entries(scheme.toJSON())
    .map(([k, v]) => `  --md-sys-color-${k.replace(/([A-Z])/g, '-$1').toLowerCase()}: ${hexFromArgb(v)};`).join('\n');
  return { light: tokens(theme.schemes.light), dark: tokens(theme.schemes.dark) };
}
