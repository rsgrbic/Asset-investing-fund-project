package main

import (
	"github.com/grafana/grafana-foundation-sdk/go/common"
	"github.com/grafana/grafana-foundation-sdk/go/prometheus"
	"github.com/grafana/grafana-foundation-sdk/go/timeseries"
)

// target is one query on a panel: the PromQL plus the legend text.
type target struct {
	expr   string
	legend string
}

// panelOpts collects the few things that differ between our panels. Everything
// else is identical, which is the whole reason this file exists.
type panelOpts struct {
	title string
	// desc becomes the little "i" tooltip in the top-left of the panel. It is
	// the only place a dashboard can carry a comment, because JSON has none.
	desc     string
	unit     string // grafana unit id: "s", "bytes", "ops", "reqps", "percentunit", "short"
	decimals *float64
	stacked  bool
	targets  []target
}

func decimals(n float64) *float64 { return &n }

// panel builds one timeseries panel. Half width, so two sit side by side.
func panel(o panelOpts) *timeseries.PanelBuilder {
	fill := 8.0
	stack := common.StackingModeNone
	if o.stacked {
		fill = 20
		stack = common.StackingModeNormal
	}

	b := timeseries.NewPanelBuilder().
		Title(o.title).
		Description(o.desc).
		Unit(o.unit).
		Span(12).
		Height(8).
		Min(0).
		FillOpacity(fill).
		ShowPoints(common.VisibilityModeNever).
		Stacking(common.NewStackingConfigBuilder().Mode(stack)).
		Legend(common.NewVizLegendOptionsBuilder().
			DisplayMode(common.LegendDisplayModeList).
			Placement(common.LegendPlacementBottom).
			ShowLegend(true)).
		Tooltip(common.NewVizTooltipOptionsBuilder().
			Mode(common.TooltipDisplayModeMulti).
			Sort(common.SortOrderDescending))

	if o.decimals != nil {
		b = b.Decimals(*o.decimals)
	}
	for _, t := range o.targets {
		b = b.WithTarget(
			prometheus.NewDataqueryBuilder().
				Expr(t.expr).
				LegendFormat(t.legend).
				Range(),
		)
	}
	return b
}
