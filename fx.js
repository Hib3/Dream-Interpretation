// 背景の星空: きらめく星と、ときどき流れる流れ星
(() => {
  const canvas = document.querySelector("#starfield");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  let width = 0;
  let height = 0;
  let stars = [];
  let shootingStars = [];
  let nextShootAt = 0;

  const STAR_COLORS = ["#ffffff", "#ffeec2", "#cdbdff", "#b8d4ff"];

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    seedStars();
  }

  function seedStars() {
    const count = Math.min(220, Math.floor((width * height) / 7000));
    stars = Array.from({ length: count }, () => ({
      x: Math.random() * width,
      y: Math.random() * height,
      r: 0.4 + Math.random() * 1.3,
      color: STAR_COLORS[Math.floor(Math.random() * STAR_COLORS.length)],
      base: 0.25 + Math.random() * 0.55,
      amp: 0.15 + Math.random() * 0.4,
      speed: 0.4 + Math.random() * 1.2,
      phase: Math.random() * Math.PI * 2,
    }));
  }

  function spawnShootingStar(now) {
    const fromX = width * (0.15 + Math.random() * 0.7);
    const angle = Math.PI * (0.62 + Math.random() * 0.18); // 左下へ流れる
    shootingStars.push({
      x: fromX,
      y: height * Math.random() * 0.35,
      vx: Math.cos(angle) * 9,
      vy: Math.sin(angle) * 9,
      life: 1,
    });
    nextShootAt = now + 4500 + Math.random() * 6500;
  }

  function drawStars(time) {
    for (const s of stars) {
      const alpha = s.base + s.amp * Math.sin(s.phase + time * 0.001 * s.speed);
      ctx.globalAlpha = Math.max(0.05, Math.min(1, alpha));
      ctx.fillStyle = s.color;
      ctx.beginPath();
      ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  function drawShootingStars() {
    for (const m of shootingStars) {
      const tail = 16;
      const grad = ctx.createLinearGradient(
        m.x,
        m.y,
        m.x - m.vx * tail,
        m.y - m.vy * tail
      );
      grad.addColorStop(0, `rgba(255, 244, 214, ${0.9 * m.life})`);
      grad.addColorStop(1, "rgba(255, 244, 214, 0)");
      ctx.strokeStyle = grad;
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      ctx.moveTo(m.x, m.y);
      ctx.lineTo(m.x - m.vx * tail, m.y - m.vy * tail);
      ctx.stroke();

      m.x += m.vx;
      m.y += m.vy;
      m.life -= 0.016;
    }
    shootingStars = shootingStars.filter(
      (m) => m.life > 0 && m.x > -100 && m.y < height + 100
    );
  }

  function frame(time) {
    ctx.clearRect(0, 0, width, height);
    drawStars(time);
    if (time > nextShootAt) spawnShootingStar(time);
    drawShootingStars();
    requestAnimationFrame(frame);
  }

  window.addEventListener("resize", resize);
  resize();

  if (reduceMotion) {
    // 動きを抑える設定では静止した星のみ描画する
    drawStars(0);
  } else {
    nextShootAt = 2500;
    requestAnimationFrame(frame);
  }
})();
