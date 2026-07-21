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

/**
 * Render an SVG line chart for equity curve data.
 * data: array of {t: "ISO-timestamp", v: number}
 * opts: { width, height, colorUp, colorDown, formatY }
 */
function renderEquityChart(containerId, data, opts) {
  var el = document.getElementById(containerId);
  if (!el) return;
  opts = opts || {};
  if (!data || data.length === 0) {
    el.innerHTML = '<div style="color:#8b949e;font-size:12px;padding:8px;">No equity data for this period.</div>';
    return;
  }

  // Single data point — show it as a label instead of a broken chart
  if (data.length === 1) {
    var v = data[0].v;
    var color = v >= 0 ? '#3fb950' : '#f85149';
    var fmt = opts.formatY || function(x) { return '$' + x.toFixed(0); };
    el.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:200px;color:' + color + ';font-size:32px;font-weight:bold;border:1px solid #21262d;border-radius:4px;background:#0d1117;">' + fmt(v) + '</div>';
    return;
  }

  var width = opts.width || el.clientWidth || 600;
  var height = opts.height || 200;
  var padding = { top: 16, right: 16, bottom: 24, left: 64 };
  var plotW = width - padding.left - padding.right;
  var plotH = height - padding.top - padding.bottom;

  var values = data.map(function (d) { return d.v; });
  var minV = Math.min.apply(null, values);
  var maxV = Math.max.apply(null, values);
  var range = maxV - minV || maxV * 0.01 || 1;
  var padRange = range * 0.08;
  minV -= padRange;
  maxV += padRange;
  range = maxV - minV;

  function yScale(v) { return padding.top + plotH * (1 - (v - minV) / range); }
  function xScale(i) { return padding.left + (i / (data.length - 1)) * plotW; }

  var colorUp = opts.colorUp || '#3fb950';
  var colorDown = opts.colorDown || '#f85149';
  var startVal = values[0];
  var endVal = values[values.length - 1];
  var lineColor = endVal >= startVal ? colorUp : colorDown;
  var fillColor = endVal >= startVal ? 'rgba(63,185,80,0.12)' : 'rgba(248,81,73,0.12)';

  function fmtY(v) {
    if (opts.formatY) return opts.formatY(v);
    if (v >= 1000) return '$' + (v / 1000).toFixed(1) + 'k';
    return '$' + v.toFixed(0);
  }

  function fmtTime(iso) {
    var d = new Date(iso);
    return (d.getMonth() + 1) + '/' + d.getDate() + ' ' + d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
  }

  var parts = [];
  parts.push(
    '<svg xmlns="http://www.w3.org/2000/svg" width="' + width + '" height="' + height +
    '" viewBox="0 0 ' + width + ' ' + height + '" style="background:#0d1117;border:1px solid #21262d;border-radius:4px;">'
  );

  // Grid lines
  var nGrid = 5;
  for (var gi = 0; gi <= nGrid; gi++) {
    var gy = padding.top + (plotH / nGrid) * gi;
    var gv = maxV - (range / nGrid) * gi;
    parts.push(
      '<line x1="' + padding.left + '" y1="' + gy + '" x2="' + (width - padding.right) + '" y2="' + gy +
      '" stroke="#21262d" stroke-width="1" />'
    );
    parts.push(
      '<text x="' + (padding.left - 6) + '" y="' + (gy + 4) + '" fill="#8b949e" font-size="10" text-anchor="end">' +
      fmtY(gv) + '</text>'
    );
  }

  // Area fill
  var areaD = '';
  for (var i = 0; i < data.length; i++) {
    var x = xScale(i);
    var y = yScale(data[i].v);
    areaD += (i === 0 ? 'M' : 'L') + x + ',' + y;
  }
  areaD += 'L' + xScale(data.length - 1) + ',' + yScale(minV) + 'L' + xScale(0) + ',' + yScale(minV) + 'Z';
  parts.push('<path d="' + areaD + '" fill="' + fillColor + '" />');

  // Line
  var lineD = '';
  for (var j = 0; j < data.length; j++) {
    var lx = xScale(j);
    var ly = yScale(data[j].v);
    lineD += (j === 0 ? 'M' : 'L') + lx + ',' + ly;
  }
  parts.push('<path d="' + lineD + '" stroke="' + lineColor + '" stroke-width="2" fill="none" stroke-linejoin="round" />');

  // Tooltip hover line (invisible wider path for interaction)
  parts.push('<path d="' + lineD + '" stroke="transparent" stroke-width="20" fill="none" style="pointer-events:stroke;" />');

  // X-axis labels (first, middle, last)
  var labelIndices = [0, Math.floor(data.length / 2), data.length - 1];
  for (var li = 0; li < labelIndices.length; li++) {
    var idx = labelIndices[li];
    var lx2 = xScale(idx);
    parts.push(
      '<text x="' + lx2 + '" y="' + (height - 4) + '" fill="#8b949e" font-size="10" text-anchor="middle">' +
      fmtTime(data[idx].t) + '</text>'
    );
  }

  // Hover dot
  parts.push(
    '<circle id="' + containerId + '-dot" r="4" fill="' + lineColor + '" stroke="#0d1117" stroke-width="2" style="display:none;" />'
  );
  parts.push(
    '<text id="' + containerId + '-tooltip" fill="#e6edf3" font-size="11" font-weight="bold" style="display:none;" />'
  );

  parts.push('</svg>');
  el.innerHTML = parts.join('');

  // Hover interaction
  var svg = el.querySelector('svg');
  if (!svg) return;
  var dot = document.getElementById(containerId + '-dot');
  var tip = document.getElementById(containerId + '-tooltip');
  if (!dot || !tip) return;

  svg.addEventListener('mousemove', function (e) {
    var rect = svg.getBoundingClientRect();
    var mx = e.clientX - rect.left - padding.left;
    var frac = mx / plotW;
    var idx = Math.round(frac * (data.length - 1));
    idx = Math.max(0, Math.min(data.length - 1, idx));
    var pt = data[idx];
    var px = xScale(idx);
    var py = yScale(pt.v);
    dot.setAttribute('cx', px);
    dot.setAttribute('cy', py);
    dot.style.display = '';
    var tipX = px < plotW / 2 ? px + 12 : px - 120;
    var tipY = py - 10;
    tip.setAttribute('x', tipX);
    tip.setAttribute('y', tipY);
    tip.textContent = fmtTime(pt.t) + '  ' + fmtY(pt.v);
    tip.style.display = '';
  });
  svg.addEventListener('mouseleave', function () {
    dot.style.display = 'none';
    tip.style.display = 'none';
  });
}

/**
 * Fetch equity history and render the portfolio chart.
 */
function renderPortfolioEquityChart(containerId, span) {
  span = span || '1w';
  fetch('/api/equity/history?span=' + span)
    .then(function (r) { return r.json(); })
    .then(function (d) {
      renderEquityChart(containerId, d.points, {
        formatY: function (v) { return '$' + v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
      });
    })
    .catch(function () {
      var el = document.getElementById(containerId);
      if (el) el.innerHTML = '<div style="color:#8b949e;font-size:12px;padding:8px;">Failed to load equity data.</div>';
    });
}

/**
 * Fetch and render a single agent's equity chart.
 */
function renderAgentEquityChart(containerId, agentName, span) {
  span = span || '1w';
  fetch('/api/agents/' + encodeURIComponent(agentName) + '/equity?span=' + span)
    .then(function (r) { return r.json(); })
    .then(function (d) {
      renderEquityChart(containerId, d.points, {
        formatY: function (v) { return '$' + v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
      });
    })
    .catch(function () {
      var el = document.getElementById(containerId);
      if (el) el.innerHTML = '<div style="color:#8b949e;font-size:12px;padding:8px;">Failed to load equity data.</div>';
    });
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { renderCandleChart: renderCandleChart, fetchTrades: fetchTrades, connectDeskWs: connectDeskWs, renderEquityChart: renderEquityChart, renderPortfolioEquityChart: renderPortfolioEquityChart, renderAgentEquityChart: renderAgentEquityChart };
}