// ── S-Bar Count custom indicator (ported from Pine Script) ────────────────────

/**
 * Build the custom-indicator descriptor list handed to the charting library
 * through `custom_indicators_getter`.
 */
export function buildCustomIndicators(PineJS) {
  return [
    {
      name: 'S-Bar Count',
      metainfo: {
        _metainfoVersion: 51,
        id: 'SBarCount@tv-basicstudies-1',
        description: 'S-Bar Count',
        shortDescription: 'S-Bar Count',
        is_price_study: false,
        isCustomIndicator: true,
        format: { type: 'price', precision: 0 },
        plots: [{ id: 'plot_0', type: 'line' }],
        defaults: {
          styles: {
            plot_0: {
              linestyle: 0,
              visible: true,
              linewidth: 1,
              plottype: 5,       // columns
              trackPrice: false,
              color: 'rgba(20, 0, 0, 0.30)',
              transparency: 0,
            }
          },
          inputs: { displayEvery: 3 }
        },
        styles: {
          plot_0: { title: 'Bar #', histogramBase: 0 }
        },
        inputs: [
          { id: 'displayEvery', name: 'Display every X bars', type: 'integer', defval: 3 },
        ],
      },
      constructor: function () {
        this.init = function (context, inputCallback) {
          this._context = context;
          this._input = inputCallback;
        };
        this.main = function (context, inputCallback) {
          this._context = context;
          this._input = inputCallback;

          var displayEvery = inputCallback(0);

          // Detect new day: dayofweek changes or first bar
          var dow = PineJS.Std.dayofweek(context);
          if (!this._prevDow) this._prevDow = context.new_var(NaN);
          var prevDow = this._prevDow.get(0);
          this._prevDow.set(dow);

          if (!this._barCount) this._barCount = context.new_var(0);
          var count = this._barCount.get(0);

          var isDaily = PineJS.Std.isdwm(context);
          if (isDaily) {
            // Daily: use day of month as count
            count = PineJS.Std.dayofmonth(context);
          } else if (isNaN(prevDow) || dow !== prevDow) {
            // New day: reset
            count = 1;
          } else {
            count = count + 1;
          }
          this._barCount.set(count);

          // Show at bar 1, then every displayEvery bars
          if (count === 1 || count % displayEvery === 0) {
            return [count];
          }
          return [NaN];
        };
      }
    }
  ];
}
