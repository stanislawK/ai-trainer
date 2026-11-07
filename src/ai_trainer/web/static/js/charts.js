// Builds the progress line chart on the History progress page (ADR-0019, ticket #517).
// Vendored Chart.js: static/vendor/chart-4.5.1.umd.min.js, v4.5.1 from the npm package
// `chart.js` (dist/chart.umd.js), sha256
// ecc3cd1eeb8c34d2178e3f59fd63ec5a3d84358c11730af0b9958dc886d7652a.
// Each <canvas data-chart="{labels, values, unit, order}"> gets one line: `values` is a number
// per day, oldest first, or null for a band with no place in the athlete's order, which is
// marked by a dashed vertical line and not plotted (B39). A band chart's `order` names its y
// axis, lightest first. Colours come from the daisyUI variables (primary for the line,
// base-content for grid and text) and the chart redraws when the theme changes.
(function () {
  "use strict";

  var charts = new Map();

  function cssColor(name) {
    var probe = document.createElement("span");
    probe.style.color = "var(" + name + ")";
    document.body.appendChild(probe);
    var color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  }

  function palette() {
    var text = cssColor("--color-base-content");
    return {
      primary: cssColor("--color-primary"),
      text: text,
      grid: "color-mix(in srgb, " + text + " 12%, transparent)",
    };
  }

  // A dashed vertical line at every day with no value.
  var markUnplaced = {
    id: "markUnplaced",
    afterDatasetsDraw: function (chart) {
      var data = chart.config.data.datasets[0].data;
      var area = chart.chartArea;
      var ctx = chart.ctx;
      ctx.save();
      ctx.setLineDash([3, 4]);
      ctx.strokeStyle = chart.options.color;
      ctx.globalAlpha = 0.4;
      ctx.lineWidth = 1;
      data.forEach(function (value, index) {
        if (value !== null) {
          return;
        }
        var x = chart.scales.x.getPixelForValue(index);
        ctx.beginPath();
        ctx.moveTo(x, area.top);
        ctx.lineTo(x, area.bottom);
        ctx.stroke();
      });
      ctx.restore();
    },
  };

  function options(spec, colors) {
    var band = spec.order.length > 0;
    var y = {
      grid: { color: colors.grid },
      ticks: { color: colors.text },
      title: { display: !band && !!spec.unit, text: spec.unit, color: colors.text },
    };
    if (band) {
      y.min = 0.5;
      y.max = spec.order.length + 0.5;
      y.ticks.stepSize = 1;
      y.ticks.callback = function (value) {
        return spec.order[value - 1] || "";
      };
    } else {
      y.beginAtZero = false;
    }
    return {
      responsive: true,
      maintainAspectRatio: false,
      color: colors.text,
      plugins: { legend: { display: false }, tooltip: { enabled: true } },
      scales: { x: { grid: { color: colors.grid }, ticks: { color: colors.text } }, y: y },
    };
  }

  function draw(canvas) {
    var spec = JSON.parse(canvas.getAttribute("data-chart"));
    var colors = palette();
    var chart = new window.Chart(canvas, {
      type: "line",
      data: {
        labels: spec.labels,
        datasets: [
          {
            data: spec.values,
            borderColor: colors.primary,
            backgroundColor: colors.primary,
            borderWidth: 2.25,
            pointRadius: 3.5,
            tension: 0,
            spanGaps: true,
          },
        ],
      },
      options: options(spec, colors),
      plugins: [markUnplaced],
    });
    charts.set(canvas, { chart: chart, spec: spec });
  }

  function redraw() {
    charts.forEach(function (entry) {
      var colors = palette();
      var dataset = entry.chart.config.data.datasets[0];
      dataset.borderColor = colors.primary;
      dataset.backgroundColor = colors.primary;
      entry.chart.options = options(entry.spec, colors);
      entry.chart.update("none");
    });
  }

  function init() {
    if (!window.Chart) {
      return;
    }
    document.querySelectorAll("canvas[data-chart]").forEach(function (canvas) {
      if (!charts.has(canvas)) {
        draw(canvas);
      }
    });
  }

  new MutationObserver(redraw).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
  init();
})();
