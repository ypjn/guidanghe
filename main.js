(() => {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  const visual = document.querySelector(".hero-visual");
  const boxes = document.querySelector(".boxes");
  if (!visual || !boxes) return;

  visual.addEventListener("pointermove", (event) => {
    const rect = visual.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width - 0.5;
    const y = (event.clientY - rect.top) / rect.height - 0.5;
    boxes.style.transform = `perspective(900px) rotateY(${x * 8}deg) rotateX(${-y * 5}deg)`;
  });

  visual.addEventListener("pointerleave", () => {
    boxes.style.transform = "";
  });
})();