/* Water motion for the ocean banners (cinemagraph).
 *
 * Draws the already-loaded banner photo into a WebGL2 canvas laid exactly
 * over it and moves only the pixels a mask allows:
 *   mask R = water  -> slow, sub-2px surface ripple + faint twinkle on glints
 *   mask G = spray  -> gentle upward drift and breathing
 * Everything else (people, board, sky, mountains) stays pixel-identical.
 *
 * Opt-in per image:  <img data-motion="hero|footer" data-motion-mask="/static/...png">
 * Skipped entirely with prefers-reduced-motion or without WebGL2; the static
 * image underneath is always there. Pauses off-screen and in hidden tabs.
 */
(function () {
  "use strict";
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  var VERT = "#version 300 es\n" +
    "in vec2 aPos; out vec2 vUv;\n" +
    "void main(){ vUv = aPos * 0.5 + 0.5; vUv.y = 1.0 - vUv.y; gl_Position = vec4(aPos, 0.0, 1.0); }";

  // 3D simplex noise (Ashima Arts / Stefan Gustavson, MIT).
  var NOISE =
    "vec3 mod289(vec3 x){return x-floor(x*(1.0/289.0))*289.0;}\n" +
    "vec4 mod289(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}\n" +
    "vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}\n" +
    "vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}\n" +
    "float snoise(vec3 v){const vec2 C=vec2(1.0/6.0,1.0/3.0);const vec4 D=vec4(0.0,0.5,1.0,2.0);" +
    "vec3 i=floor(v+dot(v,C.yyy));vec3 x0=v-i+dot(i,C.xxx);vec3 g=step(x0.yzx,x0.xyz);vec3 l=1.0-g;" +
    "vec3 i1=min(g.xyz,l.zxy);vec3 i2=max(g.xyz,l.zxy);vec3 x1=x0-i1+C.xxx;vec3 x2=x0-i2+C.yyy;vec3 x3=x0-D.yyy;" +
    "i=mod289(i);vec4 p=permute(permute(permute(i.z+vec4(0.0,i1.z,i2.z,1.0))+i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));" +
    "float n_=0.142857142857;vec3 ns=n_*D.wyz-D.xzx;vec4 j=p-49.0*floor(p*ns.z*ns.z);vec4 x_=floor(j*ns.z);vec4 y_=floor(j-7.0*x_);" +
    "vec4 x=x_*ns.x+ns.yyyy;vec4 y=y_*ns.x+ns.yyyy;vec4 h=1.0-abs(x)-abs(y);vec4 b0=vec4(x.xy,y.xy);vec4 b1=vec4(x.zw,y.zw);" +
    "vec4 s0=floor(b0)*2.0+1.0;vec4 s1=floor(b1)*2.0+1.0;vec4 sh=-step(h,vec4(0.0));vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy;vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;" +
    "vec3 p0=vec3(a0.xy,h.x);vec3 p1=vec3(a0.zw,h.y);vec3 p2=vec3(a1.xy,h.z);vec3 p3=vec3(a1.zw,h.w);" +
    "vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));p0*=norm.x;p1*=norm.y;p2*=norm.z;p3*=norm.w;" +
    "vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0);m=m*m;" +
    "return 42.0*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));}\n";

  var FRAG = "#version 300 es\nprecision highp float;\n" + NOISE +
    "in vec2 vUv; out vec4 outColor;\n" +
    "uniform sampler2D uImg; uniform sampler2D uMask;\n" +
    "uniform vec2 uScale; uniform vec2 uOffset;   // canvas uv -> image uv (object-fit: cover)\n" +
    "uniform vec2 uCss;                           // element size in CSS px\n" +
    "uniform float uT; uniform float uWater; uniform float uSpray; uniform float uGlint;\n" +
    "void main(){\n" +
    "  vec2 iuv = uOffset + vUv * uScale;\n" +
    "  vec4 m = texture(uMask, iuv);\n" +
    "  float w = m.r, s = m.g;\n" +
    "  vec2 p = vUv * uCss / 220.0;              // noise space in CSS px, resolution independent\n" +
    "  float t = uT;\n" +
    "  // water: two slow, drifting layers -> sub-2px surface ripple\n" +
    "  float a = snoise(vec3(p * vec2(1.6, 2.6) + vec2(t * 0.020, t * 0.034), t * 0.055));\n" +
    "  float b = snoise(vec3(p * vec2(3.4, 5.2) - vec2(t * 0.028, t * 0.016), t * 0.080 + 7.0));\n" +
    "  vec2 dw = vec2(a * 0.65 + b * 0.35, b * 0.55 - a * 0.25) * uWater * w;\n" +
    "  // spray: rising drift with a little turbulence\n" +
    "  float c = snoise(vec3(p * 3.0 + vec2(0.0, t * 0.09), t * 0.16 + 3.0));\n" +
    "  vec2 ds = vec2(c * 0.6, -0.55 - abs(c) * 0.45) * uSpray * s;\n" +
    "  vec2 dpx = dw + ds;                       // CSS px\n" +
    "  vec3 col = texture(uImg, iuv + (dpx / uCss) * uScale).rgb;\n" +
    "  // faint twinkle on the brightest glints only\n" +
    "  float lum = dot(col, vec3(0.299, 0.587, 0.114));\n" +
    "  float glint = smoothstep(0.80, 0.97, lum) * w;\n" +
    "  float tw = snoise(vec3(p * 26.0, t * 0.55 + 11.0));\n" +
    "  col += glint * pow(max(tw, 0.0), 3.0) * uGlint;\n" +
    "  // spray breathes very slightly\n" +
    "  col += s * 0.018 * snoise(vec3(p * 2.0, t * 0.25 + 5.0));\n" +
    "  outColor = vec4(col, 1.0);\n" +
    "}";

  var PRESETS = {
    hero:   { water: 1.35, spray: 0.0, glint: 0.10 },
    footer: { water: 0.85, spray: 2.4, glint: 0.06 },
  };

  function compile(gl, type, src) {
    var sh = gl.createShader(type); gl.shaderSource(sh, src); gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) { throw new Error(gl.getShaderInfoLog(sh)); }
    return sh;
  }

  function texture(gl, source, mip) {
    var tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.pixelStorei(gl.UNPACK_COLORSPACE_CONVERSION_WEBGL, gl.NONE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    if (mip) {
      gl.generateMipmap(gl.TEXTURE_2D);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
    } else {
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    }
    return tex;
  }

  function loadImage(src) {
    return new Promise(function (res, rej) {
      var im = new Image(); im.decoding = "async";
      im.onload = function () { res(im); }; im.onerror = rej; im.src = src;
    });
  }

  function parsePos(v) {
    var parts = (v || "50% 50%").split(/\s+/);
    var f = function (s) { return s && s.slice(-1) === "%" ? parseFloat(s) / 100 : 0.5; };
    return [f(parts[0]), f(parts[1] || parts[0])];
  }

  function Motion(img) {
    this.img = img;
    this.preset = PRESETS[img.dataset.motion] || PRESETS.hero;
    this.running = false; this.visible = false; this.ready = false;
  }

  Motion.prototype.start = function () {
    var self = this, img = this.img;
    if (this.canvas || !img.offsetParent) return;          // hidden variant: try again on toggle
    var canvas = document.createElement("canvas");
    canvas.className = "motion-canvas"; canvas.setAttribute("aria-hidden", "true");
    var gl = canvas.getContext("webgl2", { alpha: false, antialias: false, premultipliedAlpha: false, powerPreference: "low-power" });
    if (!gl) return;
    this.canvas = canvas; this.gl = gl;
    Promise.all([loadImage(img.currentSrc || img.src), loadImage(img.dataset.motionMask)]).then(function (ims) {
      var prog = gl.createProgram();
      gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VERT));
      gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, FRAG));
      gl.linkProgram(prog);
      if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
      gl.useProgram(prog);
      var buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
      var loc = gl.getAttribLocation(prog, "aPos"); gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
      gl.activeTexture(gl.TEXTURE0); texture(gl, ims[0], true);
      gl.activeTexture(gl.TEXTURE1); texture(gl, ims[1], false);
      var u = function (n) { return gl.getUniformLocation(prog, n); };
      self.u = { img: u("uImg"), mask: u("uMask"), scale: u("uScale"), offset: u("uOffset"), css: u("uCss"),
                 t: u("uT"), water: u("uWater"), spray: u("uSpray"), glint: u("uGlint") };
      gl.uniform1i(self.u.img, 0); gl.uniform1i(self.u.mask, 1);
      gl.uniform1f(self.u.water, self.preset.water); gl.uniform1f(self.u.spray, self.preset.spray);
      gl.uniform1f(self.u.glint, self.preset.glint);
      self.natural = [ims[0].naturalWidth, ims[0].naturalHeight];
      img.insertAdjacentElement("afterend", canvas);
      self.resize();
      self.ready = true;
      self.t0 = performance.now() - Math.random() * 60000;   // don't start every visit at t=0
      self.frame();                                           // first frame, then fade in
      requestAnimationFrame(function () { canvas.classList.add("is-on"); });
      new ResizeObserver(function () { self.resize(); }).observe(img);
      new IntersectionObserver(function (e) { self.visible = e[0].isIntersecting; self.kick(); },
                               { rootMargin: "100px" }).observe(img);
    }).catch(function () { if (canvas.parentNode) canvas.remove(); self.canvas = null; });
  };

  Motion.prototype.resize = function () {
    if (!this.gl) return;
    var r = this.img.getBoundingClientRect();
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
    if (this.canvas.width !== w || this.canvas.height !== h) { this.canvas.width = w; this.canvas.height = h; }
    this.gl.viewport(0, 0, w, h);
    // object-fit: cover + object-position, same as the <img> underneath
    var iw = this.natural[0], ih = this.natural[1];
    var s = Math.max(r.width / iw, r.height / ih);
    var sx = r.width / (iw * s), sy = r.height / (ih * s);
    var pos = parsePos(getComputedStyle(this.img).objectPosition);
    this.gl.uniform2f(this.u.scale, sx, sy);
    this.gl.uniform2f(this.u.offset, (1 - sx) * pos[0], (1 - sy) * pos[1]);
    this.gl.uniform2f(this.u.css, r.width, r.height);
    if (!this.running) this.frame();
  };

  Motion.prototype.frame = function () {
    var gl = this.gl;
    gl.uniform1f(this.u.t, (performance.now() - this.t0) / 1000);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  };

  Motion.prototype.kick = function () {
    var self = this;
    var should = this.ready && this.visible && !document.hidden && this.img.offsetParent !== null;
    if (should && !this.running) {
      this.running = true; var last = 0;
      var loop = function (now) {
        if (!self.running) return;
        if (now - last >= 33) { last = now; self.frame(); }   // ~30 fps is plenty for slow water
        requestAnimationFrame(loop);
      };
      requestAnimationFrame(loop);
    } else if (!should) {
      this.running = false;
    }
  };

  var motions = [];
  function scan() {
    document.querySelectorAll("img[data-motion]").forEach(function (img) {
      var m = img.__motion || (img.__motion = new Motion(img));
      if (motions.indexOf(m) < 0) motions.push(m);
      var go = function () { m.start(); m.kick(); };
      if (img.complete && img.naturalWidth) go(); else img.addEventListener("load", go, { once: true });
    });
  }
  document.addEventListener("visibilitychange", function () { motions.forEach(function (m) { m.kick(); }); });
  document.addEventListener("banner:change", function () { scan(); motions.forEach(function (m) { m.kick(); }); });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", scan); else scan();
})();
