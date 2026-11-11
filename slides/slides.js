const slides = [...document.querySelectorAll('.slide')];
const counter = document.querySelector('#counter');
let current = 0;
function show(index) {
  current = (index + slides.length) % slides.length;
  slides.forEach((slide, i) => slide.classList.toggle('active', i === current));
  counter.textContent = `${current + 1} / ${slides.length}`;
  history.replaceState(null, '', `#${current + 1}`);
}
function fromHash() { const n = Number(location.hash.slice(1)); if (n) show(n - 1); }
document.addEventListener('keydown', event => {
  if (['ArrowRight', 'ArrowDown', ' ', 'PageDown'].includes(event.key)) { event.preventDefault(); show(current + 1); }
  if (['ArrowLeft', 'ArrowUp', 'PageUp'].includes(event.key)) { event.preventDefault(); show(current - 1); }
  if (event.key === 'Home') show(0);
  if (event.key === 'End') show(slides.length - 1);
});
document.addEventListener('click', event => show(current + (event.clientX > innerWidth / 2 ? 1 : -1)));
fromHash();
