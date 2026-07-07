/* forge.js — vanilla-JS dashboard helpers. No build step, no dependencies. */

/**
 * Render a simple OHLCV candlestick chart as inline SVG into the element
 * with id `containerId`.
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

    parts.push(
      '<line x1="' + cx + '" y1="' + y(h) + '" x2="' + cx + '" y2="' + y(l) +
      '" stroke="' + color + '" stroke-width="1" />'
    );

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
 * Fetch the trade bank via /api/query with the given filter params.
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

/**
 * Connect to the desk WebSocket and update the leaderboard in-place.
 * Attaches to the leaderboard table if present on the page.
 */
function connectDeskWs() {
  var protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  var ws;
  function connect() {
    ws = new WebSocket(protocol + '//' + location.host + '/api/ws/desk');
    ws.onmessage = function(evt) {
      var agents = JSON.parse(evt.data);
      if (!agents) return;
      var tbody = document.getElementById('leaderboard-body');
      if (!tbody) return;
      var existing = {};
      var rows = tbody.querySelectorAll('tr');
      for (var i = 0; i < rows.length; i++) {
        var nameCell = rows[i].cells[0];
        if (nameCell) existing[nameCell.innerText.trim()] = rows[i];
      }
      for (var j = 0; j < agents.length; j++) {
        var a = agents[j];
        var oldRow = existing[a.name];
        var statusBadge = '<span class="badge badge-' + a.status + '">' + a.status.toUpperCase() + '</span>';
        var wrClass = a.win_rate >= 0.55 ? 'win' : (a.win_rate > 0 ? 'loss' : '');
        var pfClass = a.profit_factor >= 1.4 ? 'win' : (a.profit_factor > 0 ? 'loss' : '');
        var spClass = a.sharpe >= 1.5 ? 'win' : (a.sharpe > 0 ? 'loss' : '');
        var wrVal = a.win_rate > 0 ? (a.win_rate * 100).toFixed(1) + '%' : '\u2014';
        var pfVal = a.profit_factor > 0 ? a.profit_factor.toFixed(2) : '\u2014';
        var spVal = a.sharpe > 0 ? a.sharpe.toFixed(2) : '\u2014';
        var wrRet = a.weekly_return !== 0 ? (a.weekly_return * 100).toFixed(2) + '%' : '0.0%';
        var wrRetClass = a.weekly_return > 0 ? 'win' : (a.weekly_return < 0 ? 'loss' : '');
        var modelHtml;
        if (a.last_model_used === 'no model available') {
          modelHtml = '<span class="badge" style="background:#f85149;color:#fff;">NO MODEL AVAILABLE</span>';
        } else if (a.last_model_used) {
          modelHtml = a.last_model_used;
        } else {
          modelHtml = '\u2014';
        }
        var html = '<tr><td><a href="/agents/' + a.name + '">' + a.name + '</a></td>' +
          '<td>' + statusBadge + '</td>' +
          '<td>' + a.trades_count + '</td>' +
          '<td class="' + wrClass + '">' + wrVal + '</td>' +
          '<td class="' + pfClass + '">' + pfVal + '</td>' +
          '<td class="' + spClass + '">' + spVal + '</td>' +
          '<td class="' + wrRetClass + '">' + wrRet + '</td>' +
          '<td class="loss">' + (a.max_drawdown * 100).toFixed(1) + '%</td>' +
          '<td>' + a.open_positions_count + '</td>' +
          '<td>' + modelHtml + '</td></tr>';
        if (oldRow) {
          oldRow.outerHTML = html;
        } else {
          tbody.insertAdjacentHTML('beforeend', html);
        }
      }
      if (typeof window.sortTable === 'function' && typeof window.currentSort !== 'undefined') {
        window.sortTable(window.currentSort);
      }
    };
    ws.onclose = function() { setTimeout(connect, 5000); };
    ws.onerror = function() { ws.close(); };
  }
  connect();
}

/* Auto-connect desk WS on page load if leaderboard table exists */
if (document.getElementById('leaderboard-body')) {
  connectDeskWs();
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { renderCandleChart: renderCandleChart, fetchTrades: fetchTrades, connectDeskWs: connectDeskWs };
}