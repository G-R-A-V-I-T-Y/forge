/* forge.js — vanilla-JS dashboard helpers. No build step, no dependencies. */

/**
 * Render a simple OHLCV candlestick chart as inline SVG into the element
 * with id `containerId`.
 *
 * candles: array of [ts, open, high, low, close, volume] (the same format
 * used throughout Forge's market layer and trade fingerprints).
 */
function renderCandleChart(containerId, candles, opts) {
  var el = document.getElementById(containerId);
  if (!el) return;
  if (!candles || candles.length === 0) {
    el.innerHTML = '<div style="color:#8b949e;font-size:12px;padding:8px;">No OHLCV data for this window.</div>';
    return;
  }

  opts = opts || {};
  var width = opts.width || el.clientWidth || 600;
  var height = opts.height || 180;
  var padding = 4;

  var highs = candles.map(function (c) { return c[2]; });
  var lows = candles.map(function (c) { return c[3]; });
  var maxP = Math.max.apply(null, highs);
  var minP = Math.min.apply(null, lows);
  var range = (maxP - minP) || (maxP * 0.01) || 1;

  var n = candles.length;
  var slot = (width - padding * 2) / n;
  var bodyWidth = Math.max(1, slot * 0.6);

  function y(price) {
    return padding + (height - padding * 2) * (1 - (price - minP) / range);
  }

  var parts = [];
  parts.push(
    '<svg xmlns="http://www.w3.org/2000/svg" width="' + width + '" height="' + height +
    '" viewBox="0 0 ' + width + ' ' + height + '" style="background:#0d1117;border:1px solid #21262d;border-radius:4px;">'
  );

  for (var i = 0; i < n; i++) {
    var c = candles[i];
    var o = c[1], h = c[2], l = c[3], close = c[4];
    var cx = padding + slot * i + slot / 2;
    var up = close >= o;
    var color = up ? '#3fb950' : '#f85149';

    // wick
    parts.push(
      '<line x1="' + cx + '" y1="' + y(h) + '" x2="' + cx + '" y2="' + y(l) +
      '" stroke="' + color + '" stroke-width="1" />'
    );

    // body
    var bodyTop = y(Math.max(o, close));
    var bodyBottom = y(Math.min(o, close));
    var bodyHeight = Math.max(1, bodyBottom - bodyTop);
    parts.push(
      '<rect x="' + (cx - bodyWidth / 2) + '" y="' + bodyTop + '" width="' + bodyWidth +
      '" height="' + bodyHeight + '" fill="' + color + '" />'
    );
  }

  parts.push('</svg>');
  el.innerHTML = parts.join('');
}

/**
 * Fetch the trade bank via /api/query with the given filter params and
 * invoke `cb(tradesArray)`. Used by the /trades page filter controls.
 */
function fetchTrades(filters, cb) {
  var params = new URLSearchParams();
  Object.keys(filters || {}).forEach(function (k) {
    var v = filters[k];
    if (v !== null && v !== undefined && v !== '') params.set(k, v);
  });
  fetch('/api/query?' + params.toString())
    .then(function (r) { return r.json(); })
    .then(cb)
    .catch(function () { cb([]); });
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { renderCandleChart: renderCandleChart, fetchTrades: fetchTrades };
}
