# Portfolio Overview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an "All Games" portfolio overview tab with aggregated metrics, stacked per-game charts, combined geographic breakdowns, and merged recent reviews.

**Architecture:** New `/api/data-all` backend endpoint aggregates DB data and collector state across all games. Frontend adds an "All Games" tab (visible when 2+ games configured) that rebuilds charts as stacked datasets using per-game data, and populates metric cards with summed totals.

**Tech Stack:** Python stdlib (http.server, sqlite3), Chart.js 4.x, vanilla JS

**Important:** Do NOT run any git commands (add, commit, push). Lime commits manually.

---

### File Structure

All changes are in a single file: `C:/Programming/steam-dashboard/dashboard.py`

| Section | Lines (approx) | Changes |
|---------|----------------|---------|
| DB helper functions | ~187-192 | Add `get_all_games_sales_totals()` |
| HTTP handler `do_GET` | ~2476-2610 | Add `/api/data-all` route |
| Game selector JS | ~2126-2151 | Add "All Games" tab logic |
| Chart init JS | ~2259-2331 | Add stacked chart rebuild for portfolio mode |
| `fetchData` JS | ~2333-2433 | Add portfolio-mode fetch + render path |
| Review card rendering JS | ~2418-2426 | Add game name to review cards |

---

### Task 1: Add Backend Aggregate Query

**Files:**
- Modify: `C:/Programming/steam-dashboard/dashboard.py:187-192` (after `get_sales_totals`)

- [ ] **Step 1: Add `get_all_games_sales_totals()` function**

Add this function directly after the existing `get_sales_totals` function (after line 191):

```python
def get_all_games_sales_totals():
    conn = get_conn()
    row = conn.execute("SELECT COALESCE(SUM(units_sold),0), COALESCE(SUM(units_returned),0), COALESCE(SUM(gross_revenue_usd),0), COALESCE(SUM(net_revenue_usd),0) FROM daily_sales").fetchone()
    conn.close()
    return row
```

- [ ] **Step 2: Verify the function works**

Run in a Python shell to confirm it queries without error:

```bash
python -c "import sys; sys.path.insert(0, 'C:/Programming/steam-dashboard'); from dashboard import init_db, get_all_games_sales_totals; init_db(); print(get_all_games_sales_totals())"
```

Expected: A tuple of 4 numbers like `(500, 12, 5000.0, 3500.0)` or `(0, 0, 0.0, 0.0)` if no data.

---

### Task 2: Add `/api/data-all` Endpoint

**Files:**
- Modify: `C:/Programming/steam-dashboard/dashboard.py:2559-2607` (inside `do_GET`, before the `else: 404` block)

- [ ] **Step 1: Add the `/api/data-all` route**

Insert this new `elif` block just before the `else: self.send_response(404)` block at line 2608. This goes right after the `/api/data` block ends:

```python
        elif parsed.path == '/api/data-all':
            if not has_settings():
                self._json_response({'error': 'Not configured'}, 503)
                return

            settings = get_all_settings()
            games = settings.get('games', [])
            api_key = settings['steam_api_key']
            collector = self.server.collector

            # Aggregated sales totals
            totals_row = get_all_games_sales_totals()
            totals = {
                "units": totals_row[0], "returns": totals_row[1],
                "gross": totals_row[2], "net": totals_row[3]
            }

            # Aggregate reviews, wishlists, players, per-game chart data
            agg_reviews = {"total_positive": 0, "total_negative": 0, "total_reviews": 0}
            agg_wishlist = {"net": 0, "adds": 0, "deletes": 0, "purchases": 0}
            total_players = 0
            total_peak = 0
            per_game = {}
            all_recent_reviews = []
            merged_sales_by_country = {}
            merged_wl_by_country = {}

            for game in games:
                app_id = str(game['app_id'])
                game_name = game.get('name', app_id)
                gs = collector.get_state(app_id)

                # Players
                players = get_current_players(api_key, app_id)
                total_players += players
                total_peak += gs.peak_players

                # Reviews
                rev = get_reviews(app_id)
                agg_reviews["total_positive"] += rev.get("total_positive", 0)
                agg_reviews["total_negative"] += rev.get("total_negative", 0)
                agg_reviews["total_reviews"] += rev.get("total_reviews", 0)

                # Wishlists
                wl = gs.cached_wishlist
                agg_wishlist["net"] += wl.get("net", 0)
                agg_wishlist["adds"] += wl.get("adds", 0)
                agg_wishlist["deletes"] += wl.get("deletes", 0)
                agg_wishlist["purchases"] += wl.get("purchases", 0)

                # Per-game chart data
                per_game[app_id] = {
                    "name": game_name,
                    "daily_sales": get_all_daily_sales(app_id),
                    "player_history": get_player_history(app_id),
                    "wishlist_history": get_wishlist_history(app_id)
                }

                # Recent reviews with game name
                recent = get_recent_reviews(app_id)
                for r in recent:
                    r["game_name"] = game_name
                all_recent_reviews.extend(recent)

                # Merge sales by country
                for country, data in gs.cached_sales_by_country.items():
                    if country not in merged_sales_by_country:
                        merged_sales_by_country[country] = {}
                    for k, v in data.items():
                        merged_sales_by_country[country][k] = merged_sales_by_country[country].get(k, 0) + v

                # Merge wishlists by country
                for country, data in gs.cached_wishlist_by_country.items():
                    if country not in merged_wl_by_country:
                        merged_wl_by_country[country] = {}
                    for k, v in data.items():
                        merged_wl_by_country[country][k] = merged_wl_by_country[country].get(k, 0) + v

            # Sort recent reviews by timestamp, limit to 20
            all_recent_reviews.sort(key=lambda r: r.get("timestamp_created", 0), reverse=True)
            all_recent_reviews = all_recent_reviews[:20]

            # Sort country dicts by primary value descending
            merged_sales_by_country = dict(sorted(merged_sales_by_country.items(), key=lambda x: x[1].get("units", 0), reverse=True))
            merged_wl_by_country = dict(sorted(merged_wl_by_country.items(), key=lambda x: x[1].get("adds", 0), reverse=True))

            tg = settings.get('telegram', {})

            payload = {
                "totals": totals,
                "reviews": agg_reviews,
                "wishlist": agg_wishlist,
                "current_players": total_players,
                "peak_players": total_peak,
                "per_game": per_game,
                "sales_by_country": merged_sales_by_country,
                "wishlist_by_country": merged_wl_by_country,
                "recent_reviews": all_recent_reviews,
                "telegram_active": bool(tg.get('enabled') and tg.get('bot_token') and tg.get('chat_ids')),
                "timestamp": datetime.now().isoformat()
            }
            self._json_response(payload)
```

- [ ] **Step 2: Verify endpoint responds**

Restart the dashboard and test:

```bash
curl -s http://localhost:8081/api/data-all | python -m json.tool | head -20
```

Expected: JSON with `totals`, `reviews`, `wishlist`, `per_game`, etc. keys.

---

### Task 3: Add "All Games" Tab to Game Selector

**Files:**
- Modify: `C:/Programming/steam-dashboard/dashboard.py:2126-2151` (game selector JS)

- [ ] **Step 1: Update the game selector to include "All Games" tab**

Replace the game selector block (lines ~2129-2151) with this updated version that adds an "All Games" tab as the first item when 2+ games exist:

```javascript
  var isPortfolioMode = false;

  // Show game selector if multiple games
  if (allGames.length > 1) {
    var sel = document.getElementById('gameSelector');
    sel.classList.add('visible');

    // Add "All Games" tab first
    var allBtn = document.createElement('button');
    allBtn.className = 'game-tab';
    allBtn.textContent = T('allGames') || 'All Games';
    allBtn.setAttribute('data-appid', '__all__');
    allBtn.onclick = function() { switchGame('__all__'); };
    sel.appendChild(allBtn);

    allGames.forEach(function(g) {
      var btn = document.createElement('button');
      btn.className = 'game-tab' + (g.app_id === currentAppId ? ' active' : '');
      btn.textContent = g.name || g.app_id;
      btn.setAttribute('data-appid', g.app_id);
      btn.onclick = function() { switchGame(g.app_id); };
      sel.appendChild(btn);
    });
  }

  function switchGame(appId) {
    currentAppId = appId;
    isPortfolioMode = (appId === '__all__');
    document.getElementById('statusAppId').textContent = isPortfolioMode ? 'All Games' : appId;
    document.querySelectorAll('.game-tab').forEach(function(btn) {
      btn.classList.toggle('active', btn.getAttribute('data-appid') === appId);
    });
    document.querySelectorAll('.metric-value').forEach(function(el) { el.classList.add('loading'); });
    rebuildCharts();
    fetchData();
  }
```

- [ ] **Step 2: Add i18n entries for "allGames"**

Find the `i18n` object (around line 2153) and add `allGames` to both `ko` and `en`:

In the `ko` section, add:
```javascript
      allGames: '\uC804\uCCB4 \uAC8C\uC784',
```

In the `en` section (around line 2186), add:
```javascript
      allGames: 'All Games',
```

- [ ] **Step 3: Verify tab appears in browser**

Open http://localhost:8081 in the browser. If 2+ games are configured, the "All Games" tab should appear as the first tab in the game selector.

---

### Task 4: Update Header for Portfolio Mode

**Files:**
- Modify: `C:/Programming/steam-dashboard/dashboard.py:2333-2342` (inside `fetchData`, header update section)

- [ ] **Step 1: Add portfolio header logic to fetchData**

In the `fetchData` function, right after `fetch(url).then(...)`, add a check for portfolio mode at the top of the response handler. Replace the existing header update block:

```javascript
      // Header
      if (isPortfolioMode) {
        document.getElementById('gameName').textContent = T('allGames') || 'All Games';
        document.getElementById('gameDev').textContent = allGames.length + ' games';
        document.getElementById('headerImg').src = '';
        document.getElementById('headerImg').style.display = 'none';
        document.getElementById('gamePrice').textContent = '';
      } else {
        document.getElementById('headerImg').style.display = '';
        if (data.app_details) {
          var d = data.app_details;
          document.getElementById('gameName').textContent = d.name || '';
          document.getElementById('gameDev').textContent = (d.developers || []).join(', ') + ' \u00B7 ' + (d.publishers || []).join(', ');
          document.getElementById('headerImg').src = d.header_image || '';
          if (d.price_overview) document.getElementById('gamePrice').textContent = d.price_overview.final_formatted || '';
        }
      }
```

- [ ] **Step 2: Update the fetch URL to use the right endpoint**

Replace the `fetchData` URL line:

```javascript
  function fetchData() {
    var url = isPortfolioMode
      ? '/api/data-all'
      : '/api/data?app_id=' + encodeURIComponent(currentAppId);
```

---

### Task 5: Update Metric Cards for Portfolio Mode

**Files:**
- Modify: `C:/Programming/steam-dashboard/dashboard.py` (inside `fetchData` response handler)

- [ ] **Step 1: Update metric card population to handle both modes**

Replace the metric card update section in `fetchData` (the block that sets `totalSales`, `netRevenue`, etc.) with code that handles both response shapes:

```javascript
      document.querySelectorAll('.metric-value.loading').forEach(function(el) { el.classList.remove('loading'); });

      // Sales totals (same shape in both modes)
      var s = isPortfolioMode ? (data.totals || {}) : (data.sales_totals || {});
      var suffix = T('unitSuffix');
      document.getElementById('totalSales').textContent = (s.units || 0).toLocaleString();
      document.getElementById('salesSub').textContent = T('refunds') + ' ' + (s.returns || 0) + suffix + ' \u00B7 ' + T('grossLabel') + ' $' + (s.gross || 0).toFixed(0);
      document.getElementById('netRevenue').textContent = '$' + (s.net || 0).toLocaleString(undefined, {minimumFractionDigits: 0, maximumFractionDigits: 0});
      document.getElementById('revenueSub').textContent = T('beforeFees') + ' $' + (s.gross || 0).toFixed(0);
      document.getElementById('refundRate').textContent = (s.units > 0 ? ((s.returns / s.units) * 100).toFixed(1) : '0') + '%';

      // Players
      var players = isPortfolioMode ? (data.current_players || 0) : (data.current_players || 0);
      document.getElementById('currentPlayers').textContent = players.toLocaleString();
      document.getElementById('peakPlayers').textContent = (isPortfolioMode ? (data.peak_players || 0) : (data.peak_players || 0)).toLocaleString();

      // Reviews
      var rev = isPortfolioMode ? (data.reviews || {}) : (data.reviews || {});
      var total = rev.total_reviews || 0, pos = rev.total_positive || 0, neg = rev.total_negative || 0;
      document.getElementById('totalReviews').textContent = total;
      document.getElementById('reviewRatio').innerHTML = String.fromCodePoint(0x1F44D) + ' ' + pos + ' / ' + String.fromCodePoint(0x1F44E) + ' ' + neg;
      document.getElementById('positiveRate').textContent = total > 0 ? Math.round(pos/total*100) + '%' : '--';
      document.getElementById('reviewScore').textContent = rev.review_score_desc || '';

      // Wishlists
      var wl = isPortfolioMode ? (data.wishlist || {}) : (data.wishlist || {});
      document.getElementById('wishlistNet').textContent = '~' + (wl.net || 0).toLocaleString();
      document.getElementById('wishlistSub').textContent = '+' + (wl.adds||0) + ' / -' + (wl.deletes||0) + ' / ' + T('conversion') + ' ' + (wl.purchases||0);
```

- [ ] **Step 2: Handle player history change indicator for portfolio mode**

Replace the player change block:

```javascript
      // Player change indicator
      if (!isPortfolioMode) {
        var hist = data.player_history || [];
        if (hist.length > 1) {
          var prev = hist[hist.length - 2][1];
          var diff = players - prev;
          var el = document.getElementById('playerChange');
          el.textContent = diff > 0 ? '\u25B2 +' + diff : diff < 0 ? '\u25BC ' + diff : T('noChange');
          el.style.color = diff > 0 ? 'var(--green-bright)' : diff < 0 ? 'var(--red)' : 'var(--text-tertiary)';
        }
      } else {
        document.getElementById('playerChange').textContent = allGames.length + ' games';
        document.getElementById('playerChange').style.color = 'var(--text-tertiary)';
      }
```

---

### Task 6: Stacked Charts for Portfolio Mode

**Files:**
- Modify: `C:/Programming/steam-dashboard/dashboard.py` (chart init and fetchData sections)

This is the largest task. The `initCharts` function needs to build stacked datasets when in portfolio mode, and `fetchData` needs to populate them with per-game data.

- [ ] **Step 1: Define per-game color palette**

Add this color palette array right after the `getChartColors` function (around line 2250):

```javascript
  var gameColors = [
    { border: '#66c0f4', fill: 'rgba(102,192,244,0.3)' },
    { border: '#a4d007', fill: 'rgba(164,208,7,0.3)' },
    { border: '#c45a5a', fill: 'rgba(196,90,90,0.3)' },
    { border: '#c9a84c', fill: 'rgba(201,168,76,0.3)' },
    { border: '#7a5aaa', fill: 'rgba(122,90,170,0.3)' },
    { border: '#5ac4c4', fill: 'rgba(90,196,196,0.3)' },
    { border: '#e07850', fill: 'rgba(224,120,80,0.3)' },
    { border: '#50b050', fill: 'rgba(80,176,80,0.3)' }
  ];
```

- [ ] **Step 2: Update `initCharts` to build stacked charts in portfolio mode**

Replace the `initCharts` function with this version that handles both modes:

```javascript
  function initCharts() {
    var cc = getChartColors();
    var isMobile = window.innerWidth <= 768;
    var pr = isMobile ? 2 : 4;
    var phr = isMobile ? 3 : 6;
    var baseScaleX = {
      ticks: { color: cc.tick, maxTicksLimit: 12, font: { family: "'JetBrains Mono'", size: 10 } },
      grid: { color: cc.grid, lineWidth: 0.5 }, border: { display: false }
    };
    var baseScaleY = {
      ticks: { color: cc.tick, font: { family: "'JetBrains Mono'", size: 10 } },
      grid: { color: cc.grid, lineWidth: 0.5 }, border: { display: false }, beginAtZero: true
    };
    var baseTooltip = {
      backgroundColor: cc.tooltipBg, borderColor: cc.tooltipBorder, borderWidth: 1,
      titleFont: { family: "'Noto Sans', 'Noto Sans KR'", weight: '600' },
      bodyFont: { family: "'JetBrains Mono'", size: 12 },
      padding: 12, cornerRadius: 4, displayColors: true, boxPadding: 4
    };
    var baseOpts = {
      responsive: true,
      animation: { duration: 500, easing: 'easeOutQuart' },
      interaction: { mode: 'index', intersect: false }
    };
    var legendCfg = { display: true, labels: { color: cc.legend, usePointStyle: true, pointStyle: 'circle', padding: 16, font: { family: "'Noto Sans', 'Noto Sans KR'", size: 12 } } };

    if (isPortfolioMode) {
      // --- STACKED CUMULATIVE SALES & REVENUE ---
      // Datasets are populated dynamically in fetchData based on per_game data
      salesTimelineChart = new Chart(document.getElementById('salesTimelineChart'), {
        type: 'line',
        data: { labels: [], datasets: [] },
        options: Object.assign({}, baseOpts, {
          plugins: { legend: legendCfg, tooltip: baseTooltip },
          scales: {
            x: Object.assign({}, baseScaleX, { ticks: Object.assign({}, baseScaleX.ticks, { maxTicksLimit: 20 }) }),
            y: Object.assign({}, baseScaleY, { stacked: true, position: 'left', title: { display: !isMobile, text: T('chartSalesAxis'), color: cc.tick, font: { family: "'Noto Sans', 'Noto Sans KR'", size: 11 } } }),
            y1: Object.assign({}, baseScaleY, { stacked: true, position: 'right', grid: { drawOnChartArea: false }, title: { display: !isMobile, text: T('chartRevenueAxis'), color: cc.tick, font: { family: "'Noto Sans', 'Noto Sans KR'", size: 11 } } })
          }
        })
      });

      // --- STACKED DAILY SALES (bars) + COMBINED REVENUE (line) ---
      salesChart = new Chart(document.getElementById('salesChart'), {
        type: 'bar',
        data: { labels: [], datasets: [] },
        options: Object.assign({}, baseOpts, {
          plugins: { legend: legendCfg, tooltip: baseTooltip },
          scales: {
            x: Object.assign({}, baseScaleX, { stacked: true }),
            y: Object.assign({}, baseScaleY, { stacked: true, position: 'left', title: { display: !isMobile, text: T('chartUnits'), color: cc.tick, font: { family: "'Noto Sans', 'Noto Sans KR'", size: 11 } } }),
            y1: Object.assign({}, baseScaleY, { position: 'right', grid: { drawOnChartArea: false }, title: { display: !isMobile, text: T('chartRevenueAxis'), color: cc.tick, font: { family: "'Noto Sans', 'Noto Sans KR'", size: 11 } } })
          }
        })
      });

      // --- STACKED PLAYER ACTIVITY ---
      playerChart = new Chart(document.getElementById('playerChart'), {
        type: 'line',
        data: { labels: [], datasets: [] },
        options: Object.assign({}, baseOpts, {
          plugins: { legend: legendCfg, tooltip: baseTooltip },
          scales: {
            x: baseScaleX,
            y: Object.assign({}, baseScaleY, { stacked: true })
          }
        })
      });
    } else {
      // --- ORIGINAL SINGLE-GAME CHARTS (unchanged) ---
      salesTimelineChart = new Chart(document.getElementById('salesTimelineChart'), {
        type: 'line',
        data: { labels: [], datasets: [
          { label: T('chartCumSales'), data: [], borderColor: cc.gold, backgroundColor: cc.goldFill, fill: true, tension: 0.35, pointRadius: pr, pointHoverRadius: phr, pointBackgroundColor: cc.gold, pointBorderColor: 'transparent', borderWidth: 2.5, yAxisID: 'y' },
          { label: T('chartCumRev'), data: [], borderColor: cc.green, backgroundColor: 'transparent', borderDash: [6, 4], tension: 0.35, pointRadius: Math.max(1, pr - 1), pointHoverRadius: Math.max(2, phr - 1), pointBackgroundColor: cc.green, pointBorderColor: 'transparent', borderWidth: 2, yAxisID: 'y1' }
        ]},
        options: Object.assign({}, baseOpts, {
          plugins: { legend: legendCfg, tooltip: baseTooltip },
          scales: {
            x: Object.assign({}, baseScaleX, { ticks: Object.assign({}, baseScaleX.ticks, { maxTicksLimit: 20 }) }),
            y: Object.assign({}, baseScaleY, { position: 'left', title: { display: !isMobile, text: T('chartSalesAxis'), color: cc.tick, font: { family: "'Noto Sans', 'Noto Sans KR'", size: 11 } } }),
            y1: Object.assign({}, baseScaleY, { position: 'right', grid: { drawOnChartArea: false }, title: { display: !isMobile, text: T('chartRevenueAxis'), color: cc.tick, font: { family: "'Noto Sans', 'Noto Sans KR'", size: 11 } } })
          }
        })
      });

      salesChart = new Chart(document.getElementById('salesChart'), {
        type: 'bar',
        data: { labels: [], datasets: [
          { label: T('chartSales'), data: [], backgroundColor: cc.gold, borderRadius: 2, yAxisID: 'y', order: 2, barPercentage: 0.7 },
          { label: T('chartRefunds'), data: [], backgroundColor: cc.red, borderRadius: 2, yAxisID: 'y', order: 3, barPercentage: 0.7 },
          { label: T('chartNetRev'), data: [], type: 'line', borderColor: cc.green, backgroundColor: 'transparent', borderWidth: 2, pointRadius: Math.max(1, pr - 1), pointHoverRadius: Math.max(2, phr - 1), pointBackgroundColor: cc.green, pointBorderColor: 'transparent', tension: 0.35, yAxisID: 'y1', order: 1 }
        ]},
        options: Object.assign({}, baseOpts, {
          plugins: { legend: legendCfg, tooltip: baseTooltip },
          scales: {
            x: baseScaleX,
            y: Object.assign({}, baseScaleY, { position: 'left', title: { display: !isMobile, text: T('chartUnits'), color: cc.tick, font: { family: "'Noto Sans', 'Noto Sans KR'", size: 11 } } }),
            y1: Object.assign({}, baseScaleY, { position: 'right', grid: { drawOnChartArea: false }, title: { display: !isMobile, text: T('chartRevenueAxis'), color: cc.tick, font: { family: "'Noto Sans', 'Noto Sans KR'", size: 11 } } })
          }
        })
      });

      playerChart = new Chart(document.getElementById('playerChart'), {
        type: 'line',
        data: { labels: [], datasets: [{
          label: T('chartPlayers'), data: [],
          borderColor: cc.purple, backgroundColor: cc.purpleFill,
          fill: true, tension: 0.35, pointRadius: isMobile ? 1 : 1.5, pointHoverRadius: isMobile ? 2 : 4,
          pointBackgroundColor: cc.purple, pointBorderColor: 'transparent', borderWidth: 2
        }]},
        options: Object.assign({}, baseOpts, {
          plugins: { legend: { display: false }, tooltip: baseTooltip },
          scales: { x: baseScaleX, y: baseScaleY }
        })
      });
    }
  }
```

- [ ] **Step 3: Add portfolio chart data population in fetchData**

Add a new function `updatePortfolioCharts(data)` right before the `fetchData` function. This builds stacked datasets from `per_game` data:

```javascript
  function updatePortfolioCharts(data) {
    var perGame = data.per_game || {};
    var gameIds = Object.keys(perGame);
    var isMobile = window.innerWidth <= 768;

    // --- CUMULATIVE SALES & REVENUE (stacked area) ---
    // Collect all unique dates across all games
    var allDates = {};
    gameIds.forEach(function(id) {
      (perGame[id].daily_sales || []).forEach(function(r) {
        if (r[1] !== 0 || r[2] !== 0 || r[4] !== 0) allDates[r[0]] = true;
      });
    });
    var sortedDates = Object.keys(allDates).sort();
    var labels = sortedDates.map(function(d) { return d.substring(5); });

    var cumSalesDatasets = [];
    var cumRevDatasets = [];
    gameIds.forEach(function(id, idx) {
      var color = gameColors[idx % gameColors.length];
      var dailyByDate = {};
      (perGame[id].daily_sales || []).forEach(function(r) { dailyByDate[r[0]] = r; });

      var cumUnits = 0, cumNet = 0;
      var unitsArr = [], netArr = [];
      sortedDates.forEach(function(date) {
        var row = dailyByDate[date];
        if (row) { cumUnits += row[1]; cumNet += row[4]; }
        unitsArr.push(cumUnits);
        netArr.push(Math.round(cumNet * 100) / 100);
      });

      cumSalesDatasets.push({
        label: perGame[id].name + ' ' + T('chartSales'),
        data: unitsArr, borderColor: color.border, backgroundColor: color.fill,
        fill: true, tension: 0.35, pointRadius: isMobile ? 1 : 1.5,
        pointHoverRadius: isMobile ? 2 : 4, pointBackgroundColor: color.border,
        pointBorderColor: 'transparent', borderWidth: 2, yAxisID: 'y', stack: 'units'
      });
      cumRevDatasets.push({
        label: perGame[id].name + ' ' + T('chartNetRev'),
        data: netArr, borderColor: color.border, backgroundColor: color.fill,
        fill: true, tension: 0.35, pointRadius: isMobile ? 1 : 1.5,
        pointHoverRadius: isMobile ? 2 : 4, pointBackgroundColor: color.border,
        pointBorderColor: 'transparent', borderWidth: 2, borderDash: [4, 3],
        yAxisID: 'y1', stack: 'revenue'
      });
    });

    salesTimelineChart.data.labels = labels;
    salesTimelineChart.data.datasets = cumSalesDatasets.concat(cumRevDatasets);
    salesTimelineChart.update('none');

    // --- DAILY SALES (stacked bar) + COMBINED REVENUE (line) ---
    var dailyBarDatasets = [];
    var combinedRevArr = sortedDates.map(function() { return 0; });

    gameIds.forEach(function(id, idx) {
      var color = gameColors[idx % gameColors.length];
      var dailyByDate = {};
      (perGame[id].daily_sales || []).forEach(function(r) { dailyByDate[r[0]] = r; });

      var unitsArr = [];
      sortedDates.forEach(function(date, di) {
        var row = dailyByDate[date];
        unitsArr.push(row ? row[1] : 0);
        if (row) combinedRevArr[di] += row[4];
      });

      dailyBarDatasets.push({
        label: perGame[id].name,
        data: unitsArr, backgroundColor: color.fill, borderColor: color.border,
        borderWidth: 1, borderRadius: 2, yAxisID: 'y', stack: 'sales'
      });
    });

    // Combined net revenue line
    dailyBarDatasets.push({
      label: T('chartNetRev'),
      data: combinedRevArr.map(function(v) { return Math.round(v * 100) / 100; }),
      type: 'line', borderColor: getChartColors().green, backgroundColor: 'transparent',
      borderWidth: 2, pointRadius: isMobile ? 1 : 2, pointHoverRadius: isMobile ? 2 : 4,
      pointBackgroundColor: getChartColors().green, pointBorderColor: 'transparent',
      tension: 0.35, yAxisID: 'y1', order: 0
    });

    salesChart.data.labels = labels;
    salesChart.data.datasets = dailyBarDatasets;
    salesChart.update('none');

    // --- PLAYER ACTIVITY (stacked area) ---
    // Collect all unique timestamps across games
    var allTimestamps = {};
    gameIds.forEach(function(id) {
      (perGame[id].player_history || []).forEach(function(r) { allTimestamps[r[0]] = true; });
    });
    var sortedTimestamps = Object.keys(allTimestamps).sort();
    var playerLabels = sortedTimestamps.map(function(ts) {
      var d = new Date(ts);
      return d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
    });

    var playerDatasets = [];
    gameIds.forEach(function(id, idx) {
      var color = gameColors[idx % gameColors.length];
      var histByTs = {};
      (perGame[id].player_history || []).forEach(function(r) { histByTs[r[0]] = r[1]; });

      var playerArr = sortedTimestamps.map(function(ts) { return histByTs[ts] || 0; });

      playerDatasets.push({
        label: perGame[id].name,
        data: playerArr, borderColor: color.border, backgroundColor: color.fill,
        fill: true, tension: 0.35, pointRadius: isMobile ? 1 : 1.5,
        pointHoverRadius: isMobile ? 2 : 4, pointBackgroundColor: color.border,
        pointBorderColor: 'transparent', borderWidth: 2
      });
    });

    playerChart.data.labels = playerLabels;
    playerChart.data.datasets = playerDatasets;
    playerChart.update('none');
  }
```

- [ ] **Step 4: Wire up fetchData to call the right chart updater**

In the `fetchData` response handler, replace the chart update sections (cumulative, daily sales, player chart) with a mode branch:

```javascript
      // Charts
      if (isPortfolioMode) {
        updatePortfolioCharts(data);
      } else {
        // Original single-game chart updates
        var dailyForCum = (data.daily_sales || []).filter(function(r) { return r[1] !== 0 || r[2] !== 0 || r[4] !== 0; });
        var cumUnits = 0, cumNet = 0;
        var cumLabels = [], cumUnitsData = [], cumNetData = [];
        dailyForCum.forEach(function(r) {
          cumUnits += r[1];
          cumNet += r[4];
          cumLabels.push(r[0].substring(5));
          cumUnitsData.push(cumUnits);
          cumNetData.push(Math.round(cumNet * 100) / 100);
        });
        salesTimelineChart.data.labels = cumLabels;
        salesTimelineChart.data.datasets[0].data = cumUnitsData;
        salesTimelineChart.data.datasets[1].data = cumNetData;
        salesTimelineChart.update('none');

        var dailyRaw = data.daily_sales || [];
        var daily = dailyRaw.filter(function(r) { return r[1] !== 0 || r[2] !== 0 || r[4] !== 0; });
        salesChart.data.labels = daily.map(function(r) { return r[0].substring(5); });
        salesChart.data.datasets[0].data = daily.map(function(r) { return r[1]; });
        salesChart.data.datasets[1].data = daily.map(function(r) { return -r[2]; });
        salesChart.data.datasets[2].data = daily.map(function(r) { return r[4]; });
        salesChart.update('none');

        var hist = data.player_history || [];
        playerChart.data.labels = hist.map(function(r) { var d = new Date(r[0]); return d.getHours().toString().padStart(2,'0') + ':' + d.getMinutes().toString().padStart(2,'0'); });
        playerChart.data.datasets[0].data = hist.map(function(r) { return r[1]; });
        playerChart.update('none');
      }
```

- [ ] **Step 5: Verify stacked charts render in browser**

Open http://localhost:8081, click the "All Games" tab, verify:
- Cumulative chart shows stacked colored layers per game
- Daily sales chart shows stacked bars with a single revenue line
- Player activity chart shows stacked colored layers per game

---

### Task 7: Update Geographic Breakdowns and Recent Reviews

**Files:**
- Modify: `C:/Programming/steam-dashboard/dashboard.py` (inside `fetchData` response handler)

- [ ] **Step 1: Update country table rendering for portfolio mode**

The existing country table rendering code already works with the aggregated `sales_by_country` and `wishlist_by_country` from `/api/data-all` because it just reads the object. No changes needed to the `renderCountryTable` function or its calls — the backend already returns the merged data in the same shape.

Verify: the existing lines at ~2415-2416 work unchanged:
```javascript
      document.getElementById('salesByCountry').innerHTML = renderCountryTable(data.sales_by_country || {}, function(d) { return d.units || 0; });
      document.getElementById('wishlistByCountry').innerHTML = renderCountryTable(data.wishlist_by_country || {}, function(d) { return d.adds || 0; });
```

- [ ] **Step 2: Update recent reviews rendering to show game name**

Replace the recent reviews rendering block (around line 2418-2426) with a version that includes the game name when in portfolio mode:

```javascript
      var recent = data.recent_reviews || [];
      document.getElementById('recentReviews').innerHTML = recent.map(function(r) {
        var isUp = r.voted_up;
        var thumb = isUp ? String.fromCodePoint(0x1F44D) : String.fromCodePoint(0x1F44E);
        var thumbClass = isUp ? 'up' : 'down';
        var playtime = Math.round((r.author && r.author.playtime_forever || 0) / 60 * 10) / 10;
        var text = esc((r.review || '').substring(0, 300)).split(String.fromCharCode(10)).join(' ');
        var gameTag = (isPortfolioMode && r.game_name) ? '<span class="review-game">' + esc(r.game_name) + '</span>' : '';
        return '<div class="review-card"><div class="review-header"><span class="review-thumb ' + thumbClass + '">' + thumb + '</span>' + gameTag + '<span class="review-author">' + esc(r.author && r.author.personaname || 'Anonymous') + '</span><span class="review-playtime">' + playtime + T('hours') + '</span></div><div class="review-text">' + text + '</div></div>';
      }).join('');
```

- [ ] **Step 3: Add CSS for the game name tag in reviews**

Add this CSS rule in the `<style>` block, after the existing `.review-author` styles (around line 1940):

```css
.review-game {
  font-size: 11px; color: var(--accent); font-weight: 600;
  background: var(--accent-fill); padding: 1px 6px; border-radius: 2px;
  margin-right: 4px;
}
```

- [ ] **Step 4: Verify in browser**

Open http://localhost:8081, click "All Games" tab:
- Country tables should show aggregated data
- Recent reviews should show a colored game name badge before the author name

---

### Task 8: Final Integration Test

- [ ] **Step 1: Restart the dashboard**

Kill any running instance and restart:

```bash
taskkill //F //IM python.exe 2>/dev/null; python C:/Programming/steam-dashboard/dashboard.py &
```

- [ ] **Step 2: Verify single-game mode is unaffected**

Open http://localhost:8081, click a specific game tab. Verify all metric cards, charts, country tables, and reviews render correctly — exactly as before.

- [ ] **Step 3: Verify portfolio mode**

Click the "All Games" tab. Verify:
- Header shows "Portfolio Overview" with game count
- All 8 metric cards show aggregated values
- Cumulative chart shows stacked layers per game
- Daily chart shows stacked bars + single revenue line
- Player chart shows stacked layers per game
- Country tables show aggregated data
- Reviews show game name badges
- Switching back to a single game tab restores normal mode

- [ ] **Step 4: Verify single-game config hides the tab**

If only 1 game is configured, the "All Games" tab should not appear and the dashboard should behave identically to the original.
