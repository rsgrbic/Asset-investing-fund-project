package main

import (
	"github.com/grafana/grafana-foundation-sdk/go/common"
	"github.com/grafana/grafana-foundation-sdk/go/prometheus"
	"github.com/grafana/grafana-foundation-sdk/go/timeseries"
)

type target struct {
	expr   string
	legend string
}

type panelOpts struct {
	title string
	// Becomes the panel's "i" tooltip; the only place to annotate JSON.
	desc     string
	unit     string // grafana unit id: "s", "bytes", "ops", "reqps", "percentunit", "short"
	decimals *float64
	stacked  bool
	targets  []target
}

func decimals(n float64) *float64 { return &n }

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
