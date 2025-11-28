"""Bokeh chart generation for options trading analytics."""

from decimal import Decimal
from datetime import datetime
from bokeh.plotting import figure
from bokeh.models import (
    ColumnDataSource, HoverTool, NumeralTickFormatter, DatetimeTickFormatter,
    Span, Label, Band
)
from bokeh.embed import components
from bokeh.palettes import Category10, RdYlGn
from bokeh.transform import factor_cmap
from bokeh.layouts import column
import math


def create_cumulative_pnl_chart(data: list[tuple[datetime, Decimal]]) -> tuple[str, str]:
    """Create cumulative P&L line chart."""
    if not data:
        return create_empty_chart("Cumulative P&L", "No closed positions yet")

    dates = [d[0] for d in data]
    values = [float(d[1]) for d in data]

    source = ColumnDataSource(data={
        'date': dates,
        'pnl': values,
        'pnl_formatted': [f"${v:,.2f}" for v in values]
    })

    p = figure(
        title="Cumulative P&L Over Time",
        x_axis_type='datetime',
        height=350,
        sizing_mode='stretch_width',
        tools="pan,wheel_zoom,box_zoom,reset,save"
    )

    # Color based on positive/negative
    line_color = "#22c55e" if values[-1] >= 0 else "#ef4444"

    p.line('date', 'pnl', source=source, line_width=2, color=line_color)
    p.circle('date', 'pnl', source=source, size=6, color=line_color, alpha=0.6)

    # Zero line
    p.line(dates, [0] * len(dates), line_dash='dashed', color='gray', alpha=0.5)

    hover = HoverTool(tooltips=[
        ("Date", "@date{%F}"),
        ("Cumulative P&L", "@pnl_formatted")
    ], formatters={'@date': 'datetime'})
    p.add_tools(hover)

    p.yaxis.formatter = NumeralTickFormatter(format="$0,0")
    p.xaxis.formatter = DatetimeTickFormatter(days="%m/%d", months="%b %Y")

    _style_chart(p)
    return components(p)


def create_monthly_pnl_chart(data: list) -> tuple[str, str]:
    """Create monthly P&L bar chart."""
    if not data:
        return create_empty_chart("Monthly P&L", "No closed positions yet")

    months = [d.month for d in data]
    pnls = [float(d.pnl) for d in data]
    colors = ["#22c55e" if v >= 0 else "#ef4444" for v in pnls]

    source = ColumnDataSource(data={
        'month': months,
        'pnl': pnls,
        'pnl_formatted': [f"${v:,.2f}" for v in pnls],
        'color': colors,
        'trades': [d.num_trades for d in data],
        'winners': [d.winners for d in data],
        'losers': [d.losers for d in data]
    })

    p = figure(
        title="Monthly P&L",
        x_range=months,
        height=350,
        sizing_mode='stretch_width',
        tools="pan,wheel_zoom,box_zoom,reset,save"
    )

    p.vbar(x='month', top='pnl', source=source, width=0.7, color='color', alpha=0.8)

    # Zero line
    p.line([-0.5, len(months) - 0.5], [0, 0], line_dash='dashed', color='gray', alpha=0.5)

    hover = HoverTool(tooltips=[
        ("Month", "@month"),
        ("P&L", "@pnl_formatted"),
        ("Trades", "@trades"),
        ("W/L", "@winners / @losers")
    ])
    p.add_tools(hover)

    p.yaxis.formatter = NumeralTickFormatter(format="$0,0")
    p.xaxis.major_label_orientation = 0.7

    _style_chart(p)
    return components(p)


def create_symbol_pnl_chart(data: list) -> tuple[str, str]:
    """Create P&L by symbol horizontal bar chart."""
    if not data:
        return create_empty_chart("P&L by Symbol", "No closed positions yet")

    # Sort by P&L and take top 10
    sorted_data = sorted(data, key=lambda x: x.pnl, reverse=True)[:10]
    sorted_data.reverse()  # Reverse for horizontal bar chart

    symbols = [d.symbol for d in sorted_data]
    pnls = [float(d.pnl) for d in sorted_data]
    colors = ["#22c55e" if v >= 0 else "#ef4444" for v in pnls]

    source = ColumnDataSource(data={
        'symbol': symbols,
        'pnl': pnls,
        'pnl_formatted': [f"${v:,.2f}" for v in pnls],
        'color': colors,
        'positions': [d.num_positions for d in sorted_data],
        'win_rate': [f"{d.win_rate:.1f}%" for d in sorted_data]
    })

    p = figure(
        title="P&L by Symbol (Top 10)",
        y_range=symbols,
        height=350,
        sizing_mode='stretch_width',
        tools="pan,wheel_zoom,box_zoom,reset,save"
    )

    p.hbar(y='symbol', right='pnl', source=source, height=0.7, color='color', alpha=0.8)

    # Zero line
    p.line([0, 0], [-0.5, len(symbols) - 0.5], line_dash='dashed', color='gray', alpha=0.5)

    hover = HoverTool(tooltips=[
        ("Symbol", "@symbol"),
        ("P&L", "@pnl_formatted"),
        ("Positions", "@positions"),
        ("Win Rate", "@win_rate")
    ])
    p.add_tools(hover)

    p.xaxis.formatter = NumeralTickFormatter(format="$0,0")

    _style_chart(p)
    return components(p)


def create_win_loss_chart(winners: int, losers: int) -> tuple[str, str]:
    """Create win/loss pie chart."""
    if winners + losers == 0:
        return create_empty_chart("Win/Loss", "No closed positions yet")

    from bokeh.plotting import figure
    from math import pi

    data = {
        'category': ['Winners', 'Losers'],
        'value': [winners, losers],
        'color': ['#22c55e', '#ef4444'],
        'angle': [winners / (winners + losers) * 2 * pi, losers / (winners + losers) * 2 * pi],
        'percentage': [f"{winners / (winners + losers) * 100:.1f}%", f"{losers / (winners + losers) * 100:.1f}%"]
    }

    source = ColumnDataSource(data=data)

    p = figure(
        title=f"Win Rate: {winners / (winners + losers) * 100:.1f}%",
        height=300,
        sizing_mode='stretch_width',
        tools=""
    )

    # Calculate start and end angles
    data['start_angle'] = [0, data['angle'][0]]
    data['end_angle'] = [data['angle'][0], 2 * pi]
    source = ColumnDataSource(data=data)

    p.wedge(x=0, y=0, radius=0.9,
            start_angle='start_angle', end_angle='end_angle',
            color='color', source=source, alpha=0.8,
            legend_field='category')

    p.annular_wedge(x=0, y=0, inner_radius=0.5, outer_radius=0.9,
                    start_angle='start_angle', end_angle='end_angle',
                    color='color', source=source, alpha=0.8)

    hover = HoverTool(tooltips=[
        ("", "@category"),
        ("Count", "@value"),
        ("Percentage", "@percentage")
    ])
    p.add_tools(hover)

    p.axis.visible = False
    p.grid.visible = False
    p.legend.location = "center_right"

    _style_chart(p)
    return components(p)


def create_strategy_chart(data: dict) -> tuple[str, str]:
    """Create strategy breakdown bar chart."""
    if not data:
        return create_empty_chart("Strategy Performance", "No closed positions yet")

    strategies = list(data.keys())
    counts = [data[s]['count'] for s in strategies]
    pnls = [data[s]['pnl'] for s in strategies]
    win_rates = [data[s]['win_rate'] for s in strategies]
    colors = ["#22c55e" if p >= 0 else "#ef4444" for p in pnls]

    source = ColumnDataSource(data={
        'strategy': strategies,
        'count': counts,
        'pnl': pnls,
        'pnl_formatted': [f"${v:,.2f}" for v in pnls],
        'win_rate': [f"{wr:.1f}%" for wr in win_rates],
        'color': colors
    })

    p = figure(
        title="Strategy Performance",
        x_range=strategies,
        height=300,
        sizing_mode='stretch_width',
        tools="pan,wheel_zoom,box_zoom,reset,save"
    )

    p.vbar(x='strategy', top='pnl', source=source, width=0.6, color='color', alpha=0.8)

    hover = HoverTool(tooltips=[
        ("Strategy", "@strategy"),
        ("P&L", "@pnl_formatted"),
        ("Trades", "@count"),
        ("Win Rate", "@win_rate")
    ])
    p.add_tools(hover)

    p.yaxis.formatter = NumeralTickFormatter(format="$0,0")

    _style_chart(p)
    return components(p)


def create_empty_chart(title: str, message: str) -> tuple[str, str]:
    """Create placeholder chart when no data available."""
    p = figure(
        title=title,
        height=300,
        sizing_mode='stretch_width',
        tools=""
    )

    p.text(x=[0], y=[0], text=[message], text_align='center', text_baseline='middle',
           text_font_size='14pt', text_color='#666')

    p.axis.visible = False
    p.grid.visible = False
    p.outline_line_color = None

    _style_chart(p)
    return components(p)


def _style_chart(p):
    """Apply consistent styling to charts."""
    p.background_fill_color = "#1e1e1e"
    p.border_fill_color = "#1e1e1e"
    p.outline_line_color = "#333"
    p.title.text_color = "#e0e0e0"
    p.title.text_font_size = "14pt"

    if p.xaxis:
        p.xaxis.axis_label_text_color = "#e0e0e0"
        p.xaxis.major_label_text_color = "#b0b0b0"
        p.xaxis.axis_line_color = "#444"
        p.xaxis.major_tick_line_color = "#444"

    if p.yaxis:
        p.yaxis.axis_label_text_color = "#e0e0e0"
        p.yaxis.major_label_text_color = "#b0b0b0"
        p.yaxis.axis_line_color = "#444"
        p.yaxis.major_tick_line_color = "#444"

    if p.grid:
        p.xgrid.grid_line_color = "#333"
        p.ygrid.grid_line_color = "#333"

    if hasattr(p, 'legend') and p.legend:
        p.legend.background_fill_color = "#1e1e1e"
        p.legend.label_text_color = "#e0e0e0"
        p.legend.border_line_color = "#444"


def create_position_pnl_chart(analysis) -> tuple[str, str]:
    """
    Create P&L curve for an open position showing profit/loss at different underlying prices.
    Shows the payoff diagram at expiration.
    """
    scenarios = analysis.scenarios
    if not scenarios:
        return create_empty_chart(f"{analysis.symbol} P&L", "No scenario data")

    prices = [s.underlying_price for s in scenarios]
    pnls = [s.pnl for s in scenarios]

    # Determine colors for the P&L line segments
    colors = []
    for pnl in pnls:
        if pnl > 0:
            colors.append("#22c55e")
        elif pnl < 0:
            colors.append("#ef4444")
        else:
            colors.append("#888888")

    source = ColumnDataSource(data={
        'price': prices,
        'pnl': pnls,
        'pnl_formatted': [f"${v:,.2f}" for v in pnls],
        'color': colors
    })

    title = f"{analysis.symbol} ${analysis.strike} {analysis.option_type} - {analysis.days_to_expiry}d to expiry"

    p = figure(
        title=title,
        x_axis_label="Underlying Price at Expiration",
        y_axis_label="Profit / Loss",
        height=300,
        sizing_mode='stretch_width',
        tools="pan,wheel_zoom,box_zoom,reset,save"
    )

    # Create gradient fill for profit/loss regions
    # Add green fill above zero
    profit_prices = []
    profit_pnls = []
    loss_prices = []
    loss_pnls = []

    for i, (price, pnl) in enumerate(zip(prices, pnls)):
        if pnl >= 0:
            profit_prices.append(price)
            profit_pnls.append(pnl)
        if pnl <= 0:
            loss_prices.append(price)
            loss_pnls.append(pnl)

    # Fill profit region
    if profit_prices:
        profit_source = ColumnDataSource(data={
            'price': profit_prices,
            'pnl': profit_pnls,
            'zero': [0] * len(profit_prices)
        })
        p.varea(x='price', y1='zero', y2='pnl', source=profit_source,
                fill_color="#22c55e", fill_alpha=0.2)

    # Fill loss region
    if loss_prices:
        loss_source = ColumnDataSource(data={
            'price': loss_prices,
            'pnl': loss_pnls,
            'zero': [0] * len(loss_prices)
        })
        p.varea(x='price', y1='pnl', y2='zero', source=loss_source,
                fill_color="#ef4444", fill_alpha=0.2)

    # Main P&L line
    p.line('price', 'pnl', source=source, line_width=3, color="#3b82f6")
    p.circle('price', 'pnl', source=source, size=4, color="#3b82f6", alpha=0.6)

    # Zero line (breakeven reference)
    zero_line = Span(location=0, dimension='width', line_color='#888',
                     line_dash='dashed', line_width=1)
    p.add_layout(zero_line)

    # Strike price vertical line
    strike_line = Span(location=analysis.strike, dimension='height',
                       line_color='#f59e0b', line_dash='dotted', line_width=2)
    p.add_layout(strike_line)

    # Breakeven vertical line
    breakeven_line = Span(location=analysis.breakeven, dimension='height',
                          line_color='#a855f7', line_dash='dashed', line_width=2)
    p.add_layout(breakeven_line)

    # Add labels
    strike_label = Label(x=analysis.strike, y=max(pnls) * 0.9,
                         text=f"Strike ${analysis.strike:.2f}",
                         text_color="#f59e0b", text_font_size="10pt")
    p.add_layout(strike_label)

    breakeven_label = Label(x=analysis.breakeven, y=min(pnls) * 0.9,
                            text=f"B/E ${analysis.breakeven:.2f}",
                            text_color="#a855f7", text_font_size="10pt")
    p.add_layout(breakeven_label)

    # Current price marker (if available)
    if analysis.current_price is not None:
        current_line = Span(location=analysis.current_price, dimension='height',
                           line_color='#22d3ee', line_dash='solid', line_width=3)
        p.add_layout(current_line)

        # Add current price label and marker
        current_label = Label(x=analysis.current_price, y=max(pnls) * 0.7,
                             text=f"Current ${analysis.current_price:.2f}",
                             text_color="#22d3ee", text_font_size="10pt",
                             text_font_style="bold")
        p.add_layout(current_label)

        # Add a diamond marker at current P&L point
        if analysis.current_pnl is not None:
            p.diamond(x=[analysis.current_price], y=[analysis.current_pnl],
                     size=15, color="#22d3ee", line_color="#fff", line_width=1)

    hover = HoverTool(tooltips=[
        ("Price", "$@price{0.00}"),
        ("P&L", "@pnl_formatted")
    ])
    p.add_tools(hover)

    p.yaxis.formatter = NumeralTickFormatter(format="$0,0")
    p.xaxis.formatter = NumeralTickFormatter(format="$0.00")

    _style_chart(p)
    return components(p)


def create_combined_risk_chart(analyses: list) -> tuple[str, str]:
    """
    Create a combined chart showing all open positions' P&L ranges.
    Shows risk exposure across different underlying movements.
    """
    if not analyses:
        return create_empty_chart("Portfolio Risk", "No open positions")

    p = figure(
        title="Open Positions - P&L at Expiration by Underlying Move",
        x_axis_label="Underlying Price Change (%)",
        y_axis_label="Profit / Loss",
        height=400,
        sizing_mode='stretch_width',
        tools="pan,wheel_zoom,box_zoom,reset,save"
    )

    colors = Category10[10] if len(analyses) <= 10 else Category10[10] * 2

    # Calculate percentage moves from strike
    for i, analysis in enumerate(analyses):
        scenarios = analysis.scenarios
        if not scenarios:
            continue

        # Convert prices to percentage change from strike
        pct_changes = [(s.underlying_price / analysis.strike - 1) * 100 for s in scenarios]
        pnls = [s.pnl for s in scenarios]

        color = colors[i % len(colors)]
        label = f"{analysis.symbol} ${analysis.strike}{analysis.option_type[0]}"

        source = ColumnDataSource(data={
            'pct': pct_changes,
            'pnl': pnls,
            'symbol': [analysis.symbol] * len(pnls),
            'contract': [f"${analysis.strike} {analysis.option_type}"] * len(pnls),
            'pnl_formatted': [f"${v:,.2f}" for v in pnls]
        })

        p.line('pct', 'pnl', source=source, line_width=2, color=color,
               legend_label=label, alpha=0.8)

    # Zero line
    zero_line = Span(location=0, dimension='width', line_color='#888',
                     line_dash='dashed', line_width=1)
    p.add_layout(zero_line)

    # Current price (0% change) vertical line
    current_line = Span(location=0, dimension='height', line_color='#888',
                        line_dash='dotted', line_width=1)
    p.add_layout(current_line)

    hover = HoverTool(tooltips=[
        ("Symbol", "@symbol"),
        ("Contract", "@contract"),
        ("Price Move", "@pct{0.0}%"),
        ("P&L", "@pnl_formatted")
    ])
    p.add_tools(hover)

    p.yaxis.formatter = NumeralTickFormatter(format="$0,0")
    p.xaxis.formatter = NumeralTickFormatter(format="0%")

    p.legend.location = "top_left"
    p.legend.click_policy = "hide"

    _style_chart(p)
    return components(p)


def create_risk_summary_chart(analyses: list) -> tuple[str, str]:
    """
    Create a bar chart showing max profit vs max loss for each position.
    """
    if not analyses:
        return create_empty_chart("Risk Summary", "No open positions")

    # Filter out positions with infinite risk for display
    display_analyses = [a for a in analyses if a.max_loss > -500000]

    if not display_analyses:
        return create_empty_chart("Risk Summary", "All positions have unlimited risk")

    labels = [f"{a.symbol} ${a.strike}{a.option_type[0]}" for a in display_analyses]
    max_profits = [a.max_profit for a in display_analyses]
    max_losses = [-abs(a.max_loss) for a in display_analyses]  # Negative for display
    premiums = [a.premium_received for a in display_analyses]
    days = [a.days_to_expiry for a in display_analyses]

    source = ColumnDataSource(data={
        'label': labels,
        'max_profit': max_profits,
        'max_loss': max_losses,
        'premium': premiums,
        'days': days,
        'profit_fmt': [f"${v:,.2f}" for v in max_profits],
        'loss_fmt': [f"${abs(v):,.2f}" for v in max_losses],
        'premium_fmt': [f"${v:,.2f}" for v in premiums]
    })

    p = figure(
        title="Position Risk Profile",
        y_range=labels,
        height=max(250, len(labels) * 50),
        sizing_mode='stretch_width',
        tools="pan,wheel_zoom,box_zoom,reset,save"
    )

    # Max profit bars (right side, green)
    p.hbar(y='label', right='max_profit', source=source, height=0.3,
           color="#22c55e", alpha=0.8, legend_label="Max Profit")

    # Max loss bars (left side, red)
    p.hbar(y='label', right='max_loss', source=source, height=0.3,
           color="#ef4444", alpha=0.8, legend_label="Max Loss")

    # Premium received marker
    p.circle(x='premium', y='label', source=source, size=10,
             color="#3b82f6", legend_label="Premium")

    # Zero line
    zero_line = Span(location=0, dimension='height', line_color='#888',
                     line_dash='solid', line_width=2)
    p.add_layout(zero_line)

    hover = HoverTool(tooltips=[
        ("Position", "@label"),
        ("Max Profit", "@profit_fmt"),
        ("Max Loss", "@loss_fmt"),
        ("Premium", "@premium_fmt"),
        ("Days to Expiry", "@days")
    ])
    p.add_tools(hover)

    p.xaxis.formatter = NumeralTickFormatter(format="$0,0")
    p.legend.location = "bottom_right"

    _style_chart(p)
    return components(p)
