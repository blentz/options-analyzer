"""Bokeh chart generation for options trading analytics."""

from decimal import Decimal
from datetime import datetime
from typing import Optional
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
    p.scatter('date', 'pnl', source=source, size=6, color=line_color, alpha=0.6)

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
    """Create monthly P&L bar chart.

    Uses a datetime x-axis (not a categorical x_range) so the chart scales
    cleanly when years of data are imported. The previous categorical layout
    rendered every month as a fixed-width band, which crushed bars together
    and stacked labels into an unreadable wall once you had ~24+ months.
    Bokeh's built-in pan/zoom now works against time.
    """
    if not data:
        return create_empty_chart("Monthly P&L", "No closed positions yet")

    # MonthlyStats.month is a "YYYY-MM" string — convert to a midpoint-of-
    # month datetime for plotting, but keep the label for the hover tooltip.
    month_labels = [d.month for d in data]
    month_dates = [datetime.strptime(d.month, "%Y-%m").replace(day=15) for d in data]
    pnls = [float(d.pnl) for d in data]
    colors = ["#22c55e" if v >= 0 else "#ef4444" for v in pnls]

    source = ColumnDataSource(data={
        'date': month_dates,
        'month': month_labels,
        'pnl': pnls,
        'pnl_formatted': [f"${v:,.2f}" for v in pnls],
        'color': colors,
        'trades': [d.num_trades for d in data],
        'winners': [d.winners for d in data],
        'losers': [d.losers for d in data],
    })

    p = figure(
        title=f"Monthly P&L ({len(data)} months)",
        x_axis_type='datetime',
        height=350,
        sizing_mode='stretch_width',
        tools="pan,wheel_zoom,box_zoom,reset,save",
    )

    # Bar width is ~25 days worth of milliseconds so each month renders as
    # a fat bar without overlapping its neighbour.
    bar_width_ms = 25 * 24 * 60 * 60 * 1000
    p.vbar(x='date', top='pnl', source=source, width=bar_width_ms,
           color='color', alpha=0.8)

    # Zero line across the full date span.
    if month_dates:
        p.line([min(month_dates), max(month_dates)], [0, 0],
               line_dash='dashed', color='gray', alpha=0.5)

    hover = HoverTool(tooltips=[
        ("Month", "@month"),
        ("P&L", "@pnl_formatted"),
        ("Trades", "@trades"),
        ("W/L", "@winners / @losers"),
    ], formatters={'@date': 'datetime'})
    p.add_tools(hover)

    p.yaxis.formatter = NumeralTickFormatter(format="$0,0")
    p.xaxis.formatter = DatetimeTickFormatter(months="%b %Y", years="%Y")

    _style_chart(p)
    return components(p)


def create_symbol_pnl_chart(data: list, top_n: Optional[int] = 25) -> tuple[str, str]:
    """Create P&L by symbol horizontal bar chart with realized + unrealized split.

    Each symbol gets two stacked bars:
      - Solid: realized P&L
      - Lighter, outlined: unrealized MTM on still-held shares from
        active wheel cycles
    The total dominates sorting; hover shows the breakdown explicitly.

    `top_n` controls how many symbols are shown, ranked by |total P&L|.
    Pass `top_n=None` to show every symbol.
    """
    if not data:
        return create_empty_chart("P&L by Symbol", "No closed positions yet")

    # Detect whether SymbolStats has the new realized/unrealized fields.
    # Keep working when older callers pass simpler objects.
    has_split = hasattr(data[0], 'realized') and hasattr(data[0], 'unrealized')

    sorted_data = sorted(data, key=lambda x: abs(float(x.pnl)), reverse=True)
    if top_n is not None:
        sorted_data = sorted_data[:top_n]
    sorted_data = sorted(sorted_data, key=lambda x: float(x.pnl))

    symbols = [d.symbol for d in sorted_data]
    pnls = [float(d.pnl) for d in sorted_data]
    realized = [float(getattr(d, 'realized', d.pnl)) for d in sorted_data]
    unreal = [float(getattr(d, 'unrealized', 0)) for d in sorted_data]
    realized_colors = ["#22c55e" if v >= 0 else "#ef4444" for v in realized]
    unreal_colors = ["#3b82f6" if v >= 0 else "#f97316" for v in unreal]

    # `unreal_right` is the absolute right-edge of the unrealized bar (= realized +
    # unrealized). Bokeh needs this as a CDS column, not an inline list.
    unreal_right = [r + u for r, u in zip(realized, unreal)]
    source = ColumnDataSource(data={
        'symbol': symbols,
        'pnl': pnls,
        'pnl_formatted': [f"${v:,.2f}" for v in pnls],
        'realized': realized,
        'realized_fmt': [f"${v:,.2f}" for v in realized],
        'unreal': unreal,
        'unreal_fmt': [f"${v:,.2f}" for v in unreal],
        'unreal_right': unreal_right,
        'realized_color': realized_colors,
        'unreal_color': unreal_colors,
        'positions': [d.num_positions for d in sorted_data],
        'win_rate': [f"{d.win_rate:.1f}%" for d in sorted_data],
    })

    title_suffix = ""
    if has_split and any(u != 0 for u in unreal):
        title_suffix = " — realized (solid) + unrealized (faint)"
    title = (
        f"P&L by Symbol (Top {top_n} by |P&L|){title_suffix}"
        if top_n is not None and len(data) > top_n
        else f"P&L by Symbol ({len(symbols)} symbols){title_suffix}"
    )
    bar_height = 24
    p = figure(
        title=title,
        y_range=symbols,
        height=max(350, len(symbols) * bar_height + 80),
        sizing_mode='stretch_width',
        tools="pan,wheel_zoom,box_zoom,reset,save"
    )

    # Realized bar (solid).
    p.hbar(y='symbol', right='realized', source=source, height=0.7,
           color='realized_color', alpha=0.85, legend_label='Realized')
    # Unrealized bar (lighter, stacked from realized so totals read off the end).
    # We render this only when there's any unrealized; otherwise it'd just be a
    # no-op line at zero that confuses the legend.
    if has_split and any(u != 0 for u in unreal):
        # Stack: starts at `realized` (CDS column), extends to
        # `unreal_right` (also a CDS column = realized + unrealized).
        p.hbar(y='symbol', left='realized', right='unreal_right',
               source=source, height=0.6,
               color='unreal_color', alpha=0.45, legend_label='Unrealized')

    # Zero line
    p.line([0, 0], [-0.5, len(symbols) - 0.5], line_dash='dashed', color='gray', alpha=0.5)

    hover = HoverTool(tooltips=[
        ("Symbol", "@symbol"),
        ("Total", "@pnl_formatted"),
        ("Realized", "@realized_fmt"),
        ("Unrealized", "@unreal_fmt"),
        ("Positions", "@positions"),
        ("Win Rate", "@win_rate"),
    ])
    p.add_tools(hover)

    p.xaxis.formatter = NumeralTickFormatter(format="$0,0")
    p.legend.location = "bottom_right"
    p.legend.click_policy = "hide"
    p.legend.label_text_font_size = "8pt"

    _style_chart(p)
    return components(p)


def create_win_loss_chart(winners: int, losers: int) -> tuple[str, str]:
    """Create win/loss donut chart."""
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

    # Calculate start and end angles for each wedge
    data['start_angle'] = [0, data['angle'][0]]
    data['end_angle'] = [data['angle'][0], 2 * pi]
    
    source = ColumnDataSource(data=data)

    p = figure(
        title=f"Win Rate: {winners / (winners + losers) * 100:.1f}%",
        height=300,
        sizing_mode='stretch_width',
        tools=""
    )

    # Only render the donut (annular_wedge), not both wedge and annular_wedge
    p.annular_wedge(x=0, y=0, inner_radius=0.5, outer_radius=0.9,
                    start_angle='start_angle', end_angle='end_angle',
                    color='color', source=source, alpha=0.8,
                    legend_field='category')

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
    Shows the payoff diagram at expiration with assignment probability indicators.
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

    # Build title with assignment probability if available
    title_parts = [f"{analysis.symbol} ${analysis.strike} {analysis.option_type}"]
    title_parts.append(f"{analysis.days_to_expiry}d to expiry")
    if hasattr(analysis, 'assignment_probability') and analysis.assignment_probability is not None:
        title_parts.append(f"| {analysis.assignment_probability:.0f}% assignment risk")
    title = " - ".join(title_parts[:2])
    if len(title_parts) > 2:
        title += f" {title_parts[2]}"

    p = figure(
        title=title,
        x_axis_label="Underlying Price at Expiration",
        y_axis_label="Profit / Loss",
        height=350,  # Slightly taller to accommodate new elements
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
    p.scatter('price', 'pnl', source=source, size=4, color="#3b82f6", alpha=0.6)

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

    # 50% assignment probability line (if available and for short positions)
    if (hasattr(analysis, 'price_at_50pct_assignment') and 
        analysis.price_at_50pct_assignment is not None and
        hasattr(analysis, 'strategy') and 'SHORT' in analysis.strategy):
        
        price_50pct = analysis.price_at_50pct_assignment
        
        # Only show if within the chart range
        if min(prices) <= price_50pct <= max(prices):
            # Add vertical line at 50% assignment probability price
            fifty_pct_line = Span(location=price_50pct, dimension='height',
                                  line_color='#f97316', line_dash='dashdot', line_width=2)
            p.add_layout(fifty_pct_line)
            
            # Calculate P&L at 50% assignment price for label positioning
            mid_pnl = (max(pnls) + min(pnls)) / 2
            fifty_pct_label = Label(x=price_50pct, y=mid_pnl,
                                   text=f"50% Assign ${price_50pct:.2f}",
                                   text_color="#f97316", text_font_size="9pt",
                                   text_font_style="italic")
            p.add_layout(fifty_pct_label)
            
            # Add a triangular marker at the 50% line on the P&L curve
            # Interpolate P&L at this price
            pnl_at_50 = None
            for i in range(len(prices) - 1):
                if prices[i] <= price_50pct <= prices[i + 1]:
                    # Linear interpolation
                    t = (price_50pct - prices[i]) / (prices[i + 1] - prices[i])
                    pnl_at_50 = pnls[i] + t * (pnls[i + 1] - pnls[i])
                    break
            
            if pnl_at_50 is not None:
                p.scatter(x=[price_50pct], y=[pnl_at_50],
                         marker='triangle', size=12, color="#f97316", 
                         line_color="#fff", line_width=1)

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
            p.scatter(x=[analysis.current_price], y=[analysis.current_pnl],
                      marker='diamond', size=15, color="#22d3ee", line_color="#fff", line_width=1)

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
    p.scatter(x='premium', y='label', source=source, size=10,
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


def create_theta_decay_chart(analysis) -> tuple[str, str]:
    """
    Create a chart showing how P&L curves change over time (theta decay).
    Shows multiple P&L curves at different dates before expiration.
    """
    scenarios = analysis.scenarios
    if not scenarios:
        return create_empty_chart(f"{analysis.symbol} Time Decay", "No scenario data")

    # Use the real Black-Scholes pricing function from risk_analysis.
    # The previous implementation rolled its own approximation
    # (0.4 * σ * S * √T * "moneyness factor") which was inaccurate away from
    # the money and used ITM probability in place of delta. The proper BS
    # routine is already in the codebase — just call it.
    from app.services.risk_analysis import calculate_option_price

    prices = [s.underlying_price for s in scenarios]
    days_to_expiry = analysis.days_to_expiry
    strike = analysis.strike
    option_type = analysis.option_type
    strategy = analysis.strategy
    premium = analysis.premium_received
    num_contracts = analysis.quantity
    multiplier = 100 * num_contracts

    # Use live IV from analysis if available, otherwise default
    volatility = analysis.implied_volatility if analysis.implied_volatility else 0.30
    
    # Determine time points to show (today, 50%, 75%, at expiry)
    time_points = []
    if days_to_expiry > 0:
        time_points.append(("Today", days_to_expiry))
        if days_to_expiry > 14:
            time_points.append(("50% Time", days_to_expiry // 2))
        if days_to_expiry > 7:
            time_points.append(("75% Time", days_to_expiry // 4))
    time_points.append(("Expiry", 0))
    
    p = figure(
        title=f"{analysis.symbol} ${strike} {option_type} - Time Decay (Theta)",
        x_axis_label="Underlying Price",
        y_axis_label="Profit / Loss",
        height=350,
        sizing_mode='stretch_width',
        tools="pan,wheel_zoom,box_zoom,reset,save"
    )
    
    # Colors for different time curves (darker = closer to expiry)
    colors = ["#60a5fa", "#3b82f6", "#2563eb", "#1d4ed8"]
    
    for i, (label, dte) in enumerate(time_points):
        pnls = []
        
        for price in prices:
            # Theoretical option price per share at this underlying price and
            # remaining days-to-expiry. calculate_option_price returns
            # intrinsic when dte==0, so one branch handles both cases.
            option_value_per_share = calculate_option_price(
                option_type, price, strike, dte, volatility=volatility
            )
            estimated_option_value = option_value_per_share * multiplier

            if "SHORT" in strategy:
                # Collected premium, owe current option value
                pnl = premium - estimated_option_value
            else:
                # Paid premium, own option (would receive its value on close)
                pnl = estimated_option_value - abs(premium)

            pnls.append(pnl)
        
        color = colors[i % len(colors)]
        source = ColumnDataSource(data={
            'price': prices,
            'pnl': pnls,
            'pnl_formatted': [f"${v:,.2f}" for v in pnls],
            'time_label': [label] * len(prices),
            'dte': [dte] * len(prices)
        })
        
        # Use dashed lines for intermediate time points, solid for expiry
        line_dash = 'dashed' if dte > 0 else 'solid'
        line_width = 2 if dte > 0 else 3
        
        p.line('price', 'pnl', source=source, line_width=line_width, color=color,
               legend_label=f"{label} ({dte}d)", line_dash=line_dash, alpha=0.9)
    
    # Zero line (breakeven reference)
    zero_line = Span(location=0, dimension='width', line_color='#888',
                     line_dash='dashed', line_width=1)
    p.add_layout(zero_line)
    
    # Strike price vertical line
    strike_line = Span(location=strike, dimension='height',
                       line_color='#f59e0b', line_dash='dotted', line_width=2)
    p.add_layout(strike_line)
    
    # Current price marker
    if analysis.current_price is not None:
        current_line = Span(location=analysis.current_price, dimension='height',
                           line_color='#22d3ee', line_dash='solid', line_width=2)
        p.add_layout(current_line)
    
    hover = HoverTool(tooltips=[
        ("Time", "@time_label (@dte days)"),
        ("Price", "$@price{0.00}"),
        ("P&L", "@pnl_formatted")
    ])
    p.add_tools(hover)
    
    p.yaxis.formatter = NumeralTickFormatter(format="$0,0")
    p.xaxis.formatter = NumeralTickFormatter(format="$0.00")
    
    p.legend.location = "top_right"
    p.legend.click_policy = "hide"

    _style_chart(p)
    return components(p)


def create_speculation_pnl_chart(analysis) -> tuple[str, str]:
    """
    Create P&L curve for a speculation strategy showing profit/loss at different underlying prices.
    Similar to create_position_pnl_chart but for hypothetical strategies.
    """
    from app.services.speculation_analysis import StrategyAnalysis
    
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

    # Build title
    title = f"{analysis.symbol} - {analysis.strategy_name}"
    if analysis.days_to_expiry > 0:
        title += f" ({analysis.days_to_expiry}d to expiry)"

    p = figure(
        title=title,
        x_axis_label="Underlying Price at Expiration",
        y_axis_label="Profit / Loss",
        height=400,
        sizing_mode='stretch_width',
        tools="pan,wheel_zoom,box_zoom,reset,save"
    )

    # Create gradient fill for profit/loss regions
    profit_prices = []
    profit_pnls = []
    loss_prices = []
    loss_pnls = []

    for price, pnl in zip(prices, pnls):
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
    p.scatter('price', 'pnl', source=source, size=4, color="#3b82f6", alpha=0.6)

    # Zero line (breakeven reference)
    zero_line = Span(location=0, dimension='width', line_color='#888',
                     line_dash='dashed', line_width=1)
    p.add_layout(zero_line)

    # Current price vertical line
    if analysis.current_price is not None:
        current_line = Span(location=analysis.current_price, dimension='height',
                           line_color='#22d3ee', line_dash='solid', line_width=3)
        p.add_layout(current_line)

        current_label = Label(x=analysis.current_price, y=max(pnls) * 0.7,
                             text=f"Current ${analysis.current_price:.2f}",
                             text_color="#22d3ee", text_font_size="10pt",
                             text_font_style="bold")
        p.add_layout(current_label)

    # Breakeven lines
    for i, be in enumerate(analysis.breakeven_prices[:4]):  # Limit to 4 breakevens
        be_line = Span(location=be, dimension='height',
                       line_color='#a855f7', line_dash='dashed', line_width=2)
        p.add_layout(be_line)

        # Stagger label positions
        label_y = min(pnls) * 0.9 if i % 2 == 0 else max(pnls) * 0.9
        be_label = Label(x=be, y=label_y,
                        text=f"B/E ${be:.2f}",
                        text_color="#a855f7", text_font_size="9pt")
        p.add_layout(be_label)

    # Strike lines for each leg
    strikes = list(set(leg.strike for leg in analysis.legs))
    for strike in strikes[:4]:  # Limit to 4 strikes
        strike_line = Span(location=strike, dimension='height',
                          line_color='#f59e0b', line_dash='dotted', line_width=1)
        p.add_layout(strike_line)

    hover = HoverTool(tooltips=[
        ("Price", "$@price{0.00}"),
        ("P&L", "@pnl_formatted")
    ])
    p.add_tools(hover)

    p.yaxis.formatter = NumeralTickFormatter(format="$0,0")
    p.xaxis.formatter = NumeralTickFormatter(format="$0.00")

    _style_chart(p)
    return components(p)


def create_speculation_theta_chart(analysis) -> tuple[str, str]:
    """
    Create a chart showing how P&L curves change over time (theta decay) for speculation.

    Uses proper Black-Scholes pricing per leg and sums to get strategy P&L at
    each (price, time-remaining) point. The previous implementation tried to
    decay the strategy's NET PREMIUM by sqrt(time) and add it to intrinsic
    P&L — which double-counted (intrinsic P&L already includes the credit/
    debit) and was just incorrect for multi-leg strategies.
    """
    from app.services.speculation_analysis import calculate_strategy_pnl_at_price
    from app.services.risk_analysis import calculate_option_price

    scenarios = analysis.scenarios
    if not scenarios:
        return create_empty_chart(f"{analysis.symbol} Time Decay", "No scenario data")

    prices = [s.underlying_price for s in scenarios]
    days_to_expiry = analysis.days_to_expiry
    legs = analysis.legs

    # Use IV from analysis or default
    volatility = analysis.implied_volatility if analysis.implied_volatility else 0.30

    # Determine time points to show (today, 50%, 75%, at expiry)
    time_points = []
    if days_to_expiry > 0:
        time_points.append(("Today", days_to_expiry))
        if days_to_expiry > 14:
            time_points.append(("50% Time", days_to_expiry // 2))
        if days_to_expiry > 7:
            time_points.append(("75% Time", days_to_expiry // 4))
    time_points.append(("Expiry", 0))

    p = figure(
        title=f"{analysis.symbol} - {analysis.strategy_name} Time Decay (Theta)",
        x_axis_label="Underlying Price",
        y_axis_label="Profit / Loss",
        height=350,
        sizing_mode='stretch_width',
        tools="pan,wheel_zoom,box_zoom,reset,save"
    )

    # Colors for different time curves (lighter = further from expiry)
    colors = ["#60a5fa", "#3b82f6", "#2563eb", "#1d4ed8"]

    from datetime import date as _date, timedelta as _timedelta

    today = _date.today()

    def _strategy_pnl_with_bs(legs_, price_, eval_dte_, vol_):
        """Sum signed per-leg BS P&L using each leg's OWN remaining dte.

        For calendar/diagonal spreads the legs have different expirations —
        the short leg may already be at expiration (use intrinsic) while
        the longer-dated leg still has time value. Computing one strategy-
        wide dte would price both legs the same and produce a flat-wrong
        payoff for any non-same-expiration strategy.
        """
        eval_date = today + _timedelta(days=eval_dte_)
        total = 0.0
        for leg in legs_:
            leg_remaining = max(0, (leg.expiration - eval_date).days)
            opt_val_per_share = calculate_option_price(
                leg.option_type, price_, leg.strike, leg_remaining, volatility=vol_
            )
            multiplier = 100 * leg.quantity
            if leg.is_short:
                # Collected `premium`, owe current value
                leg_pnl = (leg.premium - opt_val_per_share) * multiplier
            else:
                # Paid `premium`, would receive current value
                leg_pnl = (opt_val_per_share - leg.premium) * multiplier
            total += leg_pnl
        return total

    for i, (label, dte) in enumerate(time_points):
        pnls = []
        for price in prices:
            if dte == 0:
                # Closed form at expiration (intrinsic only, matches existing math)
                pnls.append(calculate_strategy_pnl_at_price(legs, price))
            else:
                pnls.append(_strategy_pnl_with_bs(legs, price, dte, volatility))

        color = colors[i % len(colors)]
        source = ColumnDataSource(data={
            'price': prices,
            'pnl': pnls,
            'pnl_formatted': [f"${v:,.2f}" for v in pnls],
            'time_label': [label] * len(prices),
            'dte': [dte] * len(prices)
        })

        # Use dashed lines for intermediate time points, solid for expiry
        line_dash = 'dashed' if dte > 0 else 'solid'
        line_width = 2 if dte > 0 else 3

        p.line('price', 'pnl', source=source, line_width=line_width, color=color,
               legend_label=f"{label} ({dte}d)", line_dash=line_dash, alpha=0.9)

    # Zero line
    zero_line = Span(location=0, dimension='width', line_color='#888',
                     line_dash='dashed', line_width=1)
    p.add_layout(zero_line)

    # Current price marker
    if analysis.current_price is not None:
        current_line = Span(location=analysis.current_price, dimension='height',
                           line_color='#22d3ee', line_dash='solid', line_width=2)
        p.add_layout(current_line)

    hover = HoverTool(tooltips=[
        ("Time", "@time_label (@dte days)"),
        ("Price", "$@price{0.00}"),
        ("P&L", "@pnl_formatted")
    ])
    p.add_tools(hover)

    p.yaxis.formatter = NumeralTickFormatter(format="$0,0")
    p.xaxis.formatter = NumeralTickFormatter(format="$0.00")

    p.legend.location = "top_right"
    p.legend.click_policy = "hide"

    _style_chart(p)
    return components(p)
