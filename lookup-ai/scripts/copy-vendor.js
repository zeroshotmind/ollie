const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const vendorDir = path.join(root, 'renderer', 'vendor');
const fontsDir = path.join(vendorDir, 'fonts');

fs.mkdirSync(fontsDir, { recursive: true });

function copy(src, dest) {
  fs.copyFileSync(path.join(root, 'node_modules', src), path.join(vendorDir, dest));
}

copy('marked/lib/marked.umd.js', 'marked.min.js');
copy('katex/dist/katex.min.js', 'katex.min.js');
copy('katex/dist/katex.min.css', 'katex.min.css');
copy('katex/dist/contrib/auto-render.min.js', 'auto-render.min.js');

const fontSrcDir = path.join(root, 'node_modules', 'katex', 'dist', 'fonts');
for (const file of fs.readdirSync(fontSrcDir)) {
  if (file.endsWith('.woff2')) {
    fs.copyFileSync(path.join(fontSrcDir, file), path.join(fontsDir, file));
  }
}

console.log('Vendor assets copied to renderer/vendor');
