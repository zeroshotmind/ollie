const hole = document.getElementById('hole');
const hint = document.getElementById('hint');
const dimensions = document.getElementById('dimensions');
const darknessControl = document.getElementById('darknessControl');
const darknessSlider = document.getElementById('darknessSlider');

let dragging = false;
let locked = false;
let start = null;
let rect = null; // {left, top, width, height} once a selection exists

function applyDarkness(value) {
  document.documentElement.style.setProperty('--darkness', value);
}

window.focusOverlay.getConfig().then(({ darkness }) => {
  darknessSlider.value = darkness;
  applyDarkness(darkness);
});

darknessSlider.addEventListener('input', () => {
  applyDarkness(darknessSlider.value);
});
darknessSlider.addEventListener('change', () => {
  window.focusOverlay.setDarkness(Number(darknessSlider.value));
});

function placeHole(left, top, width, height) {
  hole.style.left = `${left}px`;
  hole.style.top = `${top}px`;
  hole.style.width = `${width}px`;
  hole.style.height = `${height}px`;
}

// Nothing selected yet: a zero-size hole still darkens the whole screen via
// the box-shadow spread, which is exactly the "everything dims" starting
// state a screenshot-style tool has before you drag anything.
placeHole(window.innerWidth / 2, window.innerHeight / 2, 0, 0);

function rectFrom(a, b) {
  return {
    left: Math.min(a.x, b.x),
    top: Math.min(a.y, b.y),
    width: Math.abs(a.x - b.x),
    height: Math.abs(a.y - b.y)
  };
}

function isInside(x, y, r) {
  return r && x >= r.left && x <= r.left + r.width && y >= r.top && y <= r.top + r.height;
}

function close() {
  window.focusOverlay.close();
}

document.addEventListener('mousedown', (e) => {
  if (e.target.closest('#darknessControl')) return; // let the slider handle its own drag
  if (locked) {
    if (isInside(e.clientX, e.clientY, rect)) return; // clicks inside the focused area are swallowed, not passed through
    close();
    return;
  }
  dragging = true;
  start = { x: e.clientX, y: e.clientY };
});

document.addEventListener('mousemove', (e) => {
  if (!dragging) return;
  const r = rectFrom(start, { x: e.clientX, y: e.clientY });
  placeHole(r.left, r.top, r.width, r.height);
  dimensions.style.display = 'block';
  dimensions.style.left = `${e.clientX + 14}px`;
  dimensions.style.top = `${e.clientY + 14}px`;
  dimensions.textContent = `${Math.round(r.width)} × ${Math.round(r.height)}`;
});

document.addEventListener('mouseup', (e) => {
  if (!dragging) return;
  dragging = false;
  dimensions.style.display = 'none';

  const r = rectFrom(start, { x: e.clientX, y: e.clientY });
  if (r.width < 6 || r.height < 6) {
    close(); // no real drag — treat like a click-to-cancel
    return;
  }
  rect = r;
  locked = true;
  hole.classList.add('locked');
  hint.classList.add('hidden');
  darknessControl.hidden = false;
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') close();
});
