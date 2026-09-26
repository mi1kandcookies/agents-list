/* The one USDC formatter for the browser; mirrors app/common/money.py.
   fmtUSDC(900) → "900 USDC", fmtUSDC(12.5) → "12.50 USDC",
   fmtUSDC(1250) → "1,250 USDC", fmtUSDC(0.000001) → "0.000001 USDC".
   Takes whole USDC (dollars). Options: { unit: false } leaves off " USDC";
   { cents: true } always shows cents, for rates such as a per-minute price. */
(function () {
  "use strict";
  function fmtUSDC(dollars, opts) {
    var micro = Math.round((Number(dollars) || 0) * 1e6);
    var frac = Math.abs(micro) % 1e6;
    var cents = !!(opts && opts.cents);
    var min = frac === 0 && !cents ? 0 : 2;
    var max = frac === 0 ? min : (frac % 1e4 === 0 ? 2 : 6);
    var text = (micro / 1e6).toLocaleString("en-US", { minimumFractionDigits: min, maximumFractionDigits: max });
    return opts && opts.unit === false ? text : text + " USDC";
  }
  window.fmtUSDC = fmtUSDC;
})();
