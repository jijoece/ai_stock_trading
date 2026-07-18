from datetime import date
from decimal import Decimal
from pathlib import Path

from dashboard.services.portfolio_service import PortfolioFilters, PortfolioService


def test_loads_persisted_book_position_price_provenance_orders_and_fills(dashboard_database: Path):
    portfolios = PortfolioService(dashboard_database).list_portfolios()

    assert len(portfolios) == 1
    portfolio = portfolios[0]
    assert portfolio.book_id == "book-1"
    assert portfolio.cash_available == Decimal("9000")
    assert portfolio.cash_reserved == Decimal("500")
    assert len(portfolio.positions) == len(portfolio.orders) == len(portfolio.fills) == 1
    position = portfolio.positions[0]
    assert position.latest_price == Decimal("100")
    assert position.price_source == "persisted-fixture-price"
    assert position.allocation_percentage == Decimal("100")


def test_filters_positions_and_activity_without_external_prices(dashboard_database: Path):
    service = PortfolioService(dashboard_database)

    assert service.list_portfolios(PortfolioFilters(symbol="ZZZ"))[0].positions == ()
    assert service.list_portfolios(PortfolioFilters(position_state="CLOSED"))[0].positions == ()
    before_activity = service.list_portfolios(PortfolioFilters(
        start_date=date(2026, 7, 18), end_date=date(2026, 7, 18)
    ))[0]
    assert before_activity.orders == ()
    assert before_activity.fills == ()
