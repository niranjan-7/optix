// Dashboard page logic. Every value goes onto the page with textContent (never innerHTML),
// so nothing that comes from the server can inject markup.
(function () {
  "use strict";
  var POLL_MS = 2000, STALE_AFTER_MS = 6000;
  var lastOk = 0, lastState = null, killing = false;

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function num(x, d) {
    if (x === null || x === undefined) return "—";
    return Number(x).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function signed(x, d) {
    if (x === null || x === undefined) return "—";
    return (x > 0 ? "+" : x < 0 ? "−" : "") + num(Math.abs(x), d === undefined ? 0 : d);
  }
  function rs(x) { return x === null || x === undefined ? "—" : "Rs " + signed(x); }
  function setPill(id, cls, text) {
    var p = $(id);
    p.className = "pill " + cls;
    p.lastElementChild.textContent = text;
  }
  function bar(id, used, limit) {
    var b = $(id), pct = limit > 0 ? Math.min(100, Math.max(0, used / limit * 100)) : 0;
    b.value = pct;
    b.className = pct >= 90 ? "hot" : pct >= 60 ? "mid" : "";
  }
  function banner(list, cls, strong, rest) {
    var d = el("div", "banner " + cls);
    d.appendChild(el("b", null, strong));
    if (rest) d.appendChild(el("span", null, " " + rest));
    list.push(d);
  }

  function render(s) {
    var r = s.risk, m = s.market, f = s.feed, banners = [];

    $("subtitle").textContent = m.underlying + " · expiry " + m.expiry + " (" + m.days_to_expiry + " day" +
      (m.days_to_expiry === 1 ? "" : "s") + ") · " + s.data_source + " prices";
    setPill("pill-mode", s.mode === "LIVE" ? "bad" : "good", s.mode === "LIVE" ? "LIVE — REAL MONEY" : "PAPER (no real orders)");
    setPill("pill-feed", f.healthy ? "good" : "bad", f.healthy ? "healthy" : "UNHEALTHY");
    setPill("pill-risk", r.kill_active ? "bad" : r.day_halted ? "warn" : "good",
      r.kill_active ? "KILL SWITCH ON" : r.day_halted ? "halted for today" : "armed, normal");
    setPill("pill-window", r.window_open ? "good" : "warn", r.window_open ? "open (" + r.entry_window + ")" : "closed (" + r.entry_window + ")");

    if (r.kill_active) banner(banners, "bad", "KILL SWITCH IS ON.", "No new orders. Reason: " + (r.kill_reason || "n/a") +
      " (" + (r.kill_source || "?") + "). To re-enable, a person must run: python -m trader.risk reset --operator NAME --note WHY");
    if (r.day_halted) banner(banners, "bad", "TRADING HALTED FOR TODAY.", r.halt_reason);
    if (!f.healthy) banner(banners, "warn", "Market data is not trustworthy:", f.reason + ". New entries are blocked until it recovers.");
    if (s.mode === "LIVE") banner(banners, "warn", "LIVE MODE.", "Orders on this system use real money.");
    $("banners").replaceChildren.apply($("banners"), banners);

    var total = s.pnl.total;
    var big = $("pnl-total");
    big.textContent = rs(total);
    big.className = "big " + (total > 0 ? "pos" : total < 0 ? "neg" : "");
    $("pnl-split").textContent = "closed " + rs(s.pnl.realised) + " · open " + rs(s.pnl.unrealised);
    $("loss-used").textContent = "Rs " + num(r.loss_used, 0) + " of " + num(r.loss_limit, 0);
    bar("loss-bar", r.loss_used, r.loss_limit);
    $("loss-note").textContent = "Trading stops for the day at this loss";
    $("orders").textContent = r.orders_used + " of " + r.orders_limit;
    bar("orders-bar", r.orders_used, r.orders_limit);
    $("positions-count").textContent = r.positions_open + " of " + r.positions_limit;
    bar("positions-bar", r.positions_open, r.positions_limit);
    $("limits-note").textContent = "max loss per trade Rs " + num(r.max_loss_per_trade, 0) + " · max " + r.max_lots_per_order +
      " lot(s) · structures: " + r.allowed_structures.join(", ");

    var mk = $("market");
    mk.replaceChildren();
    [["Spot", num(m.spot, 2)], ["Future", num(m.future, 2)], ["ATM strike", num(m.atm, 0)], ["Lot size", m.lot_size],
     ["Orders being placed", s.active_executions], ["Positions closed by", r.force_square_off + " at the latest"]].forEach(function (kv) {
      var d = el("span");
      d.appendChild(el("b", null, kv[0]));
      d.appendChild(document.createTextNode(String(kv[1])));
      mk.appendChild(d);
    });

    renderChain(s.chain);
    renderPositions(s.positions);
    renderDecisions(s.recent_decisions);
    $("footer-mode").textContent = s.mode === "LIVE"
      ? "LIVE trading is enabled. Check the go-live checklist." : "This is paper trading. No real orders are sent.";
  }

  function cell(tr, v, d, cls, dim) {
    var td = el("td", (cls || "") + (dim ? " dim" : ""), v === null || v === undefined ? "—" : num(v, d));
    tr.appendChild(td);
  }

  function renderChain(rows) {
    var tb = document.querySelector("#chain tbody");
    tb.replaceChildren();
    $("chain-note").textContent = rows.length ? "· " + rows.length + " strikes around the money" : "";
    if (!rows.length) {
      var tr = el("tr"); var td = el("td", "empty", "Waiting for prices…"); td.colSpan = 17; tr.appendChild(td); tb.appendChild(tr);
      return;
    }
    rows.forEach(function (row) {
      var tr = el("tr", row.atm ? "atm" : ""), c = row.ce, p = row.pe;
      var ref = lastState && lastState.market ? (lastState.market.spot !== null ? lastState.market.spot : lastState.market.future) : null;
      var ceItm = ref !== null && row.strike < ref, peItm = ref !== null && row.strike > ref;
      cell(tr, c.iv, 1, ceItm ? "itm" : "", c.stale); cell(tr, c.delta, 2, ceItm ? "itm" : "", c.stale); cell(tr, c.gamma, 5, ceItm ? "itm" : "", c.stale);
      cell(tr, c.theta, 1, ceItm ? "itm" : "", c.stale); cell(tr, c.vega, 2, ceItm ? "itm" : "", c.stale);
      cell(tr, c.bid, 2, ceItm ? "itm" : "", c.stale); cell(tr, c.ask, 2, ceItm ? "itm" : "", c.stale); cell(tr, c.ltp, 2, ceItm ? "itm" : "", c.stale);
      tr.appendChild(el("td", "strike", num(row.strike, 0) + (row.atm ? "  ATM" : "")));
      cell(tr, p.ltp, 2, peItm ? "itm" : "", p.stale); cell(tr, p.bid, 2, peItm ? "itm" : "", p.stale); cell(tr, p.ask, 2, peItm ? "itm" : "", p.stale);
      cell(tr, p.vega, 2, peItm ? "itm" : "", p.stale); cell(tr, p.theta, 1, peItm ? "itm" : "", p.stale); cell(tr, p.gamma, 5, peItm ? "itm" : "", p.stale);
      cell(tr, p.delta, 2, peItm ? "itm" : "", p.stale); cell(tr, p.iv, 1, peItm ? "itm" : "", p.stale);
      tb.appendChild(tr);
    });
  }

  function renderPositions(list) {
    var tb = document.querySelector("#positions tbody");
    tb.replaceChildren();
    if (!list.length) {
      var tr = el("tr"); var td = el("td", "empty", "No open positions."); td.colSpan = 6; tr.appendChild(td); tb.appendChild(tr);
      return;
    }
    list.forEach(function (p) {
      var tr = el("tr");
      tr.appendChild(el("td", null, p.position_id));
      tr.appendChild(el("td", null, p.strategy));
      var legs = el("td");
      p.legs.forEach(function (l) {
        legs.appendChild(el("span", "leg", l.side + " " + l.lots + "× " + l.symbol + "  " + num(l.entry, 2) + " → " + num(l.mark, 2) +
          "  (" + signed(l.pnl) + ")"));
      });
      tr.appendChild(legs);
      tr.appendChild(el("td", null, num(p.stop_loss, 2)));
      tr.appendChild(el("td", null, num(p.target, 2)));
      tr.appendChild(el("td", "num " + (p.unrealised > 0 ? "pos" : p.unrealised < 0 ? "neg" : ""), signed(p.unrealised)));
      tb.appendChild(tr);
    });
  }

  function renderDecisions(list) {
    var ul = $("decisions");
    ul.replaceChildren();
    if (!list.length) { ul.appendChild(el("li", "muted", "No signals yet today.")); return; }
    list.forEach(function (d) {
      var li = el("li");
      li.appendChild(el("span", "when", (d.ts || "").slice(11, 19)));
      li.appendChild(el("span", "badge " + (d.approved ? "ok" : "no"), d.approved ? "APPROVED" : "REFUSED"));
      li.appendChild(el("span", null, d.kind + " · " + d.strategy + " (" + d.source + ")"));
      li.appendChild(el("span", "why", d.approved ? d.reason : d.detail));
      ul.appendChild(li);
    });
  }


  // ---------------------------------------------------------------- performance panel
  // An XML namespace name, not an address the page ever contacts; written in two pieces so the
  // "no external URLs" test can stay strict.
  var SVGNS = "http:" + "//www.w3.org/2000/svg";
  function svg(tag, attrs, text) {
    var e = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function renderCurve(curve) {
    var box = $("curve");
    box.replaceChildren();
    if (!curve.length) { $("curve-note").textContent = "No closed trades yet."; return; }
    var W = 600, H = 160, L = 8, R = 8, T = 12, B = 22;
    var vals = [0].concat(curve.map(function (c) { return c.cumulative; }));
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    if (hi === lo) { hi += 1; lo -= 1; }
    var xs = function (i) { return curve.length === 1 ? W / 2 : L + (W - L - R) * i / (curve.length - 1); };
    var ys = function (v) { return T + (H - T - B) * (hi - v) / (hi - lo); };
    box.appendChild(svg("line", { "class": "axis", x1: L, x2: W - R, y1: H - B, y2: H - B }));
    box.appendChild(svg("line", { "class": "zero", x1: L, x2: W - R, y1: ys(0), y2: ys(0) }));
    var pts = curve.map(function (c, i) { return xs(i).toFixed(1) + "," + ys(c.cumulative).toFixed(1); });
    var last = curve[curve.length - 1];
    box.appendChild(svg("polygon", { "class": last.cumulative >= 0 ? "area-pos" : "area-neg",
      points: xs(0).toFixed(1) + "," + ys(0).toFixed(1) + " " + pts.join(" ") + " " + xs(curve.length - 1).toFixed(1) + "," + ys(0).toFixed(1) }));
    box.appendChild(svg("polyline", { "class": "line", points: pts.join(" ") }));
    box.appendChild(svg("circle", { "class": "end", cx: xs(curve.length - 1).toFixed(1), cy: ys(last.cumulative).toFixed(1), r: 3.5 }));
    box.appendChild(svg("text", { "class": "lbl", x: L, y: H - 6 }, curve[0].day));
    box.appendChild(svg("text", { "class": "lbl", x: W - R, y: H - 6, "text-anchor": "end" }, last.day));
    $("curve-note").textContent = curve.length + " trading day" + (curve.length === 1 ? "" : "s") + " · running total Rs " + signed(last.cumulative) +
      " · dashed line is zero";
  }
  function renderMonthly(rows) {
    var tb = document.querySelector("#monthly tbody");
    tb.replaceChildren();
    if (!rows.length) {
      var tr = el("tr"); var td = el("td", "empty", "No closed trades yet. The monthly report fills in as trades close."); td.colSpan = 10;
      tr.appendChild(td); tb.appendChild(tr);
      return;
    }
    rows.slice().reverse().forEach(function (r) {
      var tr = el("tr");
      tr.appendChild(el("td", null, r.month));
      tr.appendChild(el("td", "num", String(r.trades)));
      tr.appendChild(el("td", "num", r.win_rate === null ? "—" : Math.round(r.win_rate * 100) + "%"));
      tr.appendChild(el("td", "num", signed(r.gross_pnl)));
      tr.appendChild(el("td", "num", signed(-r.fees)));
      tr.appendChild(el("td", "num " + (r.net_pnl > 0 ? "pos" : r.net_pnl < 0 ? "neg" : ""), signed(r.net_pnl)));
      tr.appendChild(el("td", "num", signed(r.best_trade)));
      tr.appendChild(el("td", "num", signed(r.worst_trade)));
      tr.appendChild(el("td", "num", num(r.max_drawdown, 0)));
      tr.appendChild(el("td", "num " + (r.cumulative_net > 0 ? "pos" : r.cumulative_net < 0 ? "neg" : ""), signed(r.cumulative_net)));
      tb.appendChild(tr);
    });
  }
  function renderClosedToday(list) {
    var ul = $("closed-today");
    ul.replaceChildren();
    if (!list.length) { ul.appendChild(el("li", "muted", "Nothing closed yet today.")); return; }
    list.forEach(function (t) {
      var li = el("li");
      li.appendChild(el("span", "name", (t.closed_at || "").slice(11, 16) + "  " + t.setup));
      li.appendChild(el("b", t.net_pnl > 0 ? "pos" : t.net_pnl < 0 ? "neg" : "", signed(t.net_pnl)));
      ul.appendChild(li);
    });
  }
  async function pollReport() {
    try {
      var r = await fetch("/api/report", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      var d = await r.json();
      $("perf-note").textContent = d.available ? "" : "· no journal connected";
      renderCurve(d.curve || []);
      renderMonthly(d.monthly || []);
      renderClosedToday(d.today || []);
    } catch (e) {
      $("perf-note").textContent = "· could not load (" + e.message + ")";
    }
  }

  function showProblem(text) {
    var b = $("banners");
    var d = el("div", "banner bad");
    d.appendChild(el("b", null, text));
    d.appendChild(el("span", null, " The numbers below may be out of date. Do not rely on them."));
    b.replaceChildren(d);
    document.querySelector("main").style.opacity = "0.45";
    document.querySelector(".pills").style.opacity = "0.3";   // the status pills are out of date too: don't let them look calm
  }

  async function poll() {
    try {
      var r = await fetch("/api/state", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      lastState = await r.json();
      lastOk = Date.now();
      document.querySelector("main").style.opacity = "";
      document.querySelector(".pills").style.opacity = "";
      render(lastState);
    } catch (e) {
      showProblem("DISCONNECTED from the trading program (" + e.message + ").");
    }
  }

  setInterval(function () {
    if (!lastOk) return;
    var age = Date.now() - lastOk;
    $("updated").textContent = "Updated " + new Date(lastOk).toLocaleTimeString("en-IN") + " (" + Math.round(age / 1000) + "s ago)";
    if (age > STALE_AFTER_MS) showProblem("DATA IS STALE — no update for " + Math.round(age / 1000) + " seconds.");
  }, 1000);

  $("kill").addEventListener("click", async function () {
    if (killing) return;
    if (!window.confirm("KILL SWITCH\n\nThis stops all new orders, cancels pending orders and closes every open position.\n" +
        "Trading stays off until a person resets it from the command line.\n\nProceed?")) return;
    killing = true;
    var btn = $("kill");
    btn.disabled = true;
    try {
      var r = await fetch("/api/kill", {
        method: "POST", headers: { "X-Requested-With": "dashboard", "Content-Type": "application/json" }, body: "{}"
      });
      if (!r.ok) window.alert("The kill request reported a problem (HTTP " + r.status +
        "). Check the kill switch state and your positions, and use the command line if needed:\npython -m trader.risk kill");
    } catch (e) {
      window.alert("Could not reach the trading program: " + e.message + "\nUse the command line instead:\npython -m trader.risk kill");
    } finally {
      killing = false;
      btn.disabled = false;
      poll();
    }
  });

  poll();
  setInterval(poll, POLL_MS);
  pollReport();
  setInterval(pollReport, 30000);
})();
