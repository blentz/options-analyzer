"""Debug router: low-level scraper inspection endpoints.

These return raw scraped HTML, screenshots, intercepted API requests, and
other internals. Disabled by default via DebugGateMiddleware in main.py.

All endpoints here were previously inline in main.py. They were extracted
verbatim — only the decorator was swapped from @app.* to @router.* and
the runtime imports that were inline-imported per-call have been moved
to module level where straightforward.

If you add a debug route, add it here, not in main.py. The middleware
will only gate routes under /api/debug.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter(prefix="/api/debug", tags=["debug"])

@router.get("/stocknear/{symbol}")
async def debug_stocknear(symbol: str, db: AsyncSession = Depends(get_db)):
    """
    Debug endpoint to view raw scraped content from StockNear.
    Useful for debugging regex patterns.
    """
    from app.stocknear import StockNearScraper
    import asyncio
    
    def scrape_sync():
        with StockNearScraper() as scraper:
            # Get the options overview page content
            scraper.navigate(f"/stocks/{symbol.lower()}/options")
            scraper.page.wait_for_timeout(3000)
            overview_content = scraper.get_page_text()
            
            # Get the chain page content
            scraper.navigate(f"/stocks/{symbol.lower()}/options/chain")
            scraper.page.wait_for_timeout(3000)
            chain_content = scraper.get_page_text()
            
            return {
                "overview_url": f"https://stocknear.com/stocks/{symbol.lower()}/options",
                "overview_content": overview_content,
                "overview_length": len(overview_content),
                "chain_url": f"https://stocknear.com/stocks/{symbol.lower()}/options/chain",
                "chain_content": chain_content,
                "chain_length": len(chain_content),
            }
    
    from app.services.stocknear_service import run_scraper
    result = await run_scraper(scrape_sync)
    return result


@router.get("/stocknear-api/{symbol}")
async def debug_stocknear_api(
    symbol: str,
    expiration: str,
    strike: float,
    option_type: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint to scrape the contract lookup page using dropdown selection.
    Selects the expiration, strike, and option type, then waits for data to load.
    """
    from app.stocknear import StockNearScraper
    import asyncio
    import json
    
    def scrape_sync():
        with StockNearScraper() as scraper:
            contract_id = scraper._build_contract_id(symbol, expiration, option_type, strike)
            
            # Intercept API requests AND responses to see headers and data
            api_requests = []
            api_responses = []
            
            def handle_request(request):
                if "options-contract" in request.url.lower():
                    try:
                        api_requests.append({
                            "url": request.url,
                            "method": request.method,
                            "headers": dict(request.headers) if request.headers else {},
                            "post_data": request.post_data
                        })
                    except Exception as e:
                        api_requests.append({"error": str(e)})
            
            def handle_response(response):
                if "options-contract" in response.url.lower():
                    try:
                        body = response.text()
                        api_responses.append({
                            "url": response.url,
                            "status": response.status,
                            "body_preview": body[:1000] if body else None
                        })
                    except:
                        api_responses.append({
                            "url": response.url,
                            "status": response.status,
                            "body_preview": "failed to get body"
                        })
            
            scraper.page.on("request", handle_request)
            scraper.page.on("response", handle_response)
            
            # Navigate to the contract lookup page
            url = f"/stocks/{symbol.lower()}/options/contract-lookup"
            scraper.navigate(url)
            scraper.page.wait_for_timeout(3000)
            
            selection_log = []
            
            # Select option type first (Call/Put)
            type_text = "Call" if option_type.upper() == "CALL" else "Put"
            try:
                # Click the Option Type dropdown button
                type_btn = scraper.page.locator('text=Option Type').locator('..').locator('button').first
                if type_btn.count() > 0:
                    type_btn.click()
                    scraper.page.wait_for_timeout(500)
                    # Select the option type
                    scraper.page.locator(f'[role="menuitem"]:has-text("{type_text}")').first.click()
                    scraper.page.wait_for_timeout(1000)
                    selection_log.append(f"Selected option type: {type_text}")
            except Exception as e:
                selection_log.append(f"Option type selection failed: {str(e)[:100]}")
            
            # Select expiration date
            try:
                exp_btn = scraper.page.locator('text=Date Expiration').locator('..').locator('button').first
                if exp_btn.count() > 0:
                    exp_btn.click()
                    scraper.page.wait_for_timeout(500)
                    # Find and click the expiration - format like "Mar 20, 2026"
                    scraper.page.locator(f'[role="menuitem"]:has-text("{expiration}")').first.click()
                    scraper.page.wait_for_timeout(1000)
                    selection_log.append(f"Selected expiration: {expiration}")
            except Exception as e:
                selection_log.append(f"Expiration selection failed: {str(e)[:100]}")
            
            # Select strike price
            strike_str = str(int(strike)) if strike == int(strike) else str(strike)
            try:
                strike_btn = scraper.page.locator('text=Strike Price').locator('..').locator('button').first
                if strike_btn.count() > 0:
                    strike_btn.click()
                    scraper.page.wait_for_timeout(500)
                    # Select the strike
                    scraper.page.locator(f'[role="menuitem"]:has-text("{strike_str}")').first.click()
                    scraper.page.wait_for_timeout(2000)
                    selection_log.append(f"Selected strike: {strike_str}")
            except Exception as e:
                selection_log.append(f"Strike selection failed: {str(e)[:100]}")
            
            # Wait for data to load after selections
            try:
                scraper.page.wait_for_function(
                    """() => {
                        const text = document.body.innerText;
                        return text.includes('Last') && 
                               text.includes('Bid') && 
                               text.includes('Ask') &&
                               text.length > 1500;
                    }""",
                    timeout=15000
                )
                selection_log.append("Data loaded successfully")
            except Exception as e:
                selection_log.append(f"Wait for data timeout: {str(e)[:100]}")
            
            scraper.page.wait_for_timeout(2000)
            
            # Get the full page content
            content = scraper.get_page_text()
            
            # Take screenshot
            screenshot_path: Optional[str] = f"/app/data/debug_api_{contract_id}.png"
            try:
                scraper.screenshot(screenshot_path)
            except Exception:
                screenshot_path = None
            
            # Parse quote data from the rendered page
            import re
            quote_data = {}
            
            # Last price
            last_match = re.search(r'\bLast\s*[\n\r]+\s*\$?(\d+\.?\d*)', content, re.IGNORECASE)
            if last_match:
                quote_data['last'] = float(last_match.group(1))
            
            # Bid
            bid_match = re.search(r'\bBid\s*[\n\r]+\s*\$?(\d+\.?\d*)', content, re.IGNORECASE)
            if bid_match:
                quote_data['bid'] = float(bid_match.group(1))
            
            # Mid
            mid_match = re.search(r'\bMid\s*[\n\r]+\s*\$?(\d+\.?\d*)', content, re.IGNORECASE)
            if mid_match:
                quote_data['mid'] = float(mid_match.group(1))
            
            # Ask
            ask_match = re.search(r'\bAsk\s*[\n\r]+\s*\$?(\d+\.?\d*)', content, re.IGNORECASE)
            if ask_match:
                quote_data['ask'] = float(ask_match.group(1))
            
            # Volume
            vol_match = re.search(r'\bVolume\s*[\n\r]+\s*([\d,]+)', content, re.IGNORECASE)
            if vol_match:
                quote_data['volume'] = int(vol_match.group(1).replace(',', ''))
            
            # Open Interest
            oi_match = re.search(r'\bOpen\s*Interest\s*[\n\r]+\s*([\d,]+)', content, re.IGNORECASE)
            if oi_match:
                quote_data['open_interest'] = int(oi_match.group(1).replace(',', ''))
            
            # IV
            iv_match = re.search(r'\bImplied\s*Volatility\s*\(?IV\)?\s*[\n\r]+\s*(\d+\.?\d*)%?', content, re.IGNORECASE)
            if iv_match:
                quote_data['iv'] = float(iv_match.group(1)) / 100
            
            return {
                "contract_id": contract_id,
                "selection_log": selection_log,
                "api_requests": api_requests[:5],
                "api_responses": api_responses,
                "quote_data": quote_data,
                "page_text_length": len(content),
                "page_sample": content[:4000],
                "screenshot_path": screenshot_path
            }
    
    from app.services.stocknear_service import run_scraper
    result = await run_scraper(scrape_sync)
    return result


@router.get("/contract-js/{symbol}")
async def debug_contract_js(
    symbol: str,
    expiration: str,
    strike: float,
    option_type: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint to analyze the contract lookup page's JavaScript and network calls.
    """
    from app.stocknear import StockNearScraper
    import asyncio
    import json
    
    def scrape_sync():
        with StockNearScraper() as scraper:
            contract_id = scraper._build_contract_id(symbol, expiration, option_type, strike)
            url = f"/stocks/{symbol.lower()}/options/contract-lookup?contract={contract_id}"
            
            # Set up network request interception to capture API calls
            api_calls = []
            
            def handle_request(request):
                if "api" in request.url.lower() or "query" in request.url.lower() or "fetch" in request.url.lower():
                    api_calls.append({
                        "url": request.url,
                        "method": request.method,
                        "resource_type": request.resource_type
                    })
            
            def handle_response(response):
                if "api" in response.url.lower() or "query" in response.url.lower():
                    try:
                        api_calls.append({
                            "url": response.url,
                            "status": response.status,
                            "response_type": "response"
                        })
                    except:
                        pass
            
            scraper.page.on("request", handle_request)
            scraper.page.on("response", handle_response)
            
            scraper.navigate(url)
            
            # Wait longer and scroll to trigger lazy loading
            scraper.page.wait_for_timeout(3000)
            
            # Try scrolling to trigger any lazy-loaded content
            scraper.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            scraper.page.wait_for_timeout(2000)
            
            # Check for any fetch/XHR calls in the page
            js_info = scraper.page.evaluate("""() => {
                const scripts = Array.from(document.querySelectorAll('script')).map(s => s.src || 'inline');
                
                // Look for relevant data in window object
                const windowKeys = Object.keys(window).filter(k => 
                    k.toLowerCase().includes('data') || 
                    k.toLowerCase().includes('option') ||
                    k.toLowerCase().includes('contract') ||
                    k.toLowerCase().includes('quote')
                );
                
                // Check for __NEXT_DATA__ (if Next.js)
                let nextData = null;
                if (window.__NEXT_DATA__) {
                    nextData = JSON.stringify(window.__NEXT_DATA__).substring(0, 2000);
                }
                
                // Check for SvelteKit/Svelte stores
                let svelteData = null;
                try {
                    // Look for any svelte-related data
                    const svelteElements = document.querySelectorAll('[data-svelte-h]');
                    svelteData = svelteElements.length > 0 ? svelteElements.length + " svelte elements" : null;
                } catch(e) {}
                
                return {
                    scripts: scripts.slice(0, 10),
                    windowKeys: windowKeys.slice(0, 20),
                    nextData: nextData,
                    svelteData: svelteData,
                    bodyLength: document.body.innerText.length,
                    hasLogin: document.body.innerText.includes('Login'),
                    hasNoData: document.body.innerText.includes('No data'),
                    pageSample: document.body.innerText.substring(0, 1500)
                };
            }""")
            
            content = scraper.get_page_text()
            html = scraper.page.content()
            
            # Look for API endpoints in the HTML/JS
            import re
            api_patterns = re.findall(r'["\'](/api/[^"\']+)["\']', html)[:20]
            fetch_patterns = re.findall(r'fetch\(["\']([^"\']+)["\']', html)[:20]
            
            # Take screenshot
            screenshot_path: Optional[str] = f"/app/data/debug_js_{contract_id}.png"
            try:
                scraper.screenshot(screenshot_path)
            except Exception:
                screenshot_path = None
            
            return {
                "contract_id": contract_id,
                "url": f"https://stocknear.com{url}",
                "api_calls_captured": api_calls[:20],
                "js_info": js_info,
                "api_patterns_in_html": api_patterns,
                "fetch_patterns_in_html": fetch_patterns,
                "page_text_length": len(content),
                "screenshot_path": screenshot_path
            }
    
    from app.services.stocknear_service import run_scraper
    result = await run_scraper(scrape_sync)
    return result


@router.get("/contract/{symbol}")
async def debug_contract(
    symbol: str,
    expiration: str,
    strike: float,
    option_type: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint to see raw content from a specific contract lookup page.
    Shows exactly what StockNear returns.
    """
    from app.stocknear import StockNearScraper
    import asyncio
    
    def scrape_sync():
        with StockNearScraper() as scraper:
            contract_id = scraper._build_contract_id(symbol, expiration, option_type, strike)
            url = f"/stocks/{symbol.lower()}/options/contract-lookup?contract={contract_id}"
            
            scraper.navigate(url)
            
            # Try to wait for price data to load
            wait_result = "none"
            try:
                # Wait for any of these indicators that price data loaded
                scraper.page.wait_for_function(
                    """() => {
                        const text = document.body.innerText;
                        return text.includes('Bid') && text.includes('Ask') ||
                               text.includes('Last') && text.includes('Volume') ||
                               text.includes('No data is available');
                    }""",
                    timeout=15000
                )
                wait_result = "price_data_or_no_data"
            except Exception as e:
                wait_result = f"timeout: {str(e)[:100]}"
            
            scraper.page.wait_for_timeout(2000)  # Extra settle time
            
            content = scraper.get_page_text()
            html = scraper.page.content()  # Get full HTML
            
            # Search for price-related content in HTML
            import re
            bid_in_html = bool(re.search(r'["\']bid["\']', html, re.I))
            ask_in_html = bool(re.search(r'["\']ask["\']', html, re.I))
            price_patterns = re.findall(r'\$[\d.]+', content)[:10]
            
            # Check for API responses in page
            api_data_match = re.search(r'__NEXT_DATA__[^>]*>([^<]+)<', html)
            next_data_preview = api_data_match.group(1)[:500] if api_data_match else None
            
            # Take screenshot
            screenshot_path: Optional[str] = f"/app/data/debug_{contract_id}.png"
            try:
                scraper.screenshot(screenshot_path)
            except Exception:
                screenshot_path = None
            
            return {
                "contract_id": contract_id,
                "url": f"https://stocknear.com{url}",
                "page_text": content,
                "page_text_length": len(content),
                "html_length": len(html),
                "first_1000_chars": content[:1000],
                "screenshot_path": screenshot_path,
                "wait_result": wait_result,
                "bid_in_html": bid_in_html,
                "ask_in_html": ask_in_html,
                "price_patterns_found": price_patterns,
                "next_data_preview": next_data_preview
            }
    
    from app.services.stocknear_service import run_scraper
    result = await run_scraper(scrape_sync)
    return result


@router.get("/yahoo-option/{symbol}")
async def debug_yahoo_option(
    symbol: str,
    expiration: str,
    strike: float,
    option_type: str
):
    """
    Debug endpoint to test Yahoo Finance options API.
    """
    from app.services.price_service import get_option_quote, get_option_chain
    
    # Get the specific quote
    quote = await get_option_quote(symbol, expiration, strike, option_type)
    
    if quote:
        return {
            "status": "found",
            "contract_symbol": quote.contract_symbol,
            "underlying": quote.underlying,
            "strike": quote.strike,
            "expiration": quote.expiration,
            "option_type": quote.option_type,
            "bid": quote.bid,
            "ask": quote.ask,
            "last": quote.last,
            "volume": quote.volume,
            "open_interest": quote.open_interest,
            "implied_volatility": quote.implied_volatility,
            "in_the_money": quote.in_the_money
        }
    else:
        return {
            "status": "not_found",
            "symbol": symbol,
            "expiration": expiration,
            "strike": strike,
            "option_type": option_type
        }


@router.get("/quote/{symbol}")
async def debug_quote_method(
    symbol: str,
    expiration: str,
    strike: float,
    option_type: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint that tests the get_contract_quote method with dropdown selection.
    """
    from app.stocknear import StockNearScraper
    import asyncio
    
    def scrape_sync():
        with StockNearScraper() as scraper:
            quote = scraper.get_contract_quote(symbol, expiration, strike, option_type)
            return {
                "contract_id": quote.contract_id,
                "symbol": quote.symbol,
                "expiration": quote.expiration,
                "strike": quote.strike,
                "option_type": quote.option_type,
                "bid": quote.bid,
                "ask": quote.ask,
                "mid": quote.mid,
                "last": quote.last,
                "volume": quote.volume,
                "open_interest": quote.open_interest,
                "iv": quote.implied_volatility,
                "delta": quote.delta,
                "raw_content_length": len(quote.raw_content) if quote.raw_content else 0,
                "raw_content_sample": quote.raw_content[:2000] if quote.raw_content else None
            }
    
    from app.services.stocknear_service import run_scraper
    result = await run_scraper(scrape_sync)
    return result


@router.get("/oi/{symbol}")
async def debug_oi(
    symbol: str,
    expiration: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint to see the OI page content for a specific expiration.
    This page may contain bid/ask for each strike.
    """
    from app.stocknear import StockNearScraper
    import asyncio
    import re
    
    def scrape_sync():
        with StockNearScraper() as scraper:
            # Navigate to OI page
            url = f"/stocks/{symbol.lower()}/options/oi"
            scraper.navigate(url)
            
            # Wait for data to load
            try:
                scraper.page.wait_for_function(
                    """() => {
                        const text = document.body.innerText;
                        return text.includes('STRIKE') || 
                               text.includes('CALL OI') || 
                               text.includes('PUT OI') ||
                               text.length > 3000;
                    }""",
                    timeout=15000
                )
            except Exception as e:
                pass
            
            scraper.page.wait_for_timeout(2000)
            
            # If expiration specified, try to click on it
            if expiration:
                try:
                    scraper.page.click(f"text={expiration}", timeout=5000)
                    scraper.page.wait_for_timeout(3000)
                except Exception as e:
                    pass
            
            content = scraper.get_page_text()
            html = scraper.page.content()
            
            # Take screenshot
            screenshot_path: Optional[str] = f"/app/data/debug_oi_{symbol}.png"
            try:
                scraper.screenshot(screenshot_path)
            except Exception:
                screenshot_path = None
            
            return {
                "symbol": symbol.upper(),
                "expiration": expiration,
                "url": f"https://stocknear.com{url}",
                "page_text_length": len(content),
                "html_length": len(html),
                "sample_content": content[:4000],
                "screenshot_path": screenshot_path
            }
    
    from app.services.stocknear_service import run_scraper
    result = await run_scraper(scrape_sync)
    return result


@router.get("/greeks/{symbol}")
async def debug_greeks(
    symbol: str,
    expiration: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint to see the Greeks page content for a specific expiration.
    This page contains bid/ask for each strike.
    """
    from app.stocknear import StockNearScraper
    import asyncio
    import re
    
    def scrape_sync():
        with StockNearScraper() as scraper:
            # Navigate to Greeks page
            url = f"/stocks/{symbol.lower()}/options/greeks"
            scraper.navigate(url)
            
            # Wait for data to load
            try:
                scraper.page.wait_for_function(
                    """() => {
                        const text = document.body.innerText;
                        return text.includes('STRIKE') || 
                               text.includes('BID') || 
                               text.includes('ASK') ||
                               text.includes('DELTA');
                    }""",
                    timeout=15000
                )
            except Exception as e:
                pass
            
            scraper.page.wait_for_timeout(2000)
            
            # If expiration specified, try to click on it
            if expiration:
                try:
                    scraper.page.click(f"text={expiration}", timeout=5000)
                    scraper.page.wait_for_timeout(3000)
                except Exception as e:
                    pass
            
            content = scraper.get_page_text()
            html = scraper.page.content()
            
            # Search for bid/ask patterns
            # Format might be: STRIKE | BID | ASK | LAST | ... or similar
            bid_ask_pattern = re.findall(r'(\d+\.?\d*)\s+(\d+\.\d+)\s+(\d+\.\d+)', content)[:20]
            
            # Take screenshot
            screenshot_path: Optional[str] = f"/app/data/debug_greeks_{symbol}.png"
            try:
                scraper.screenshot(screenshot_path)
            except Exception:
                screenshot_path = None
            
            return {
                "symbol": symbol.upper(),
                "expiration": expiration,
                "url": f"https://stocknear.com{url}",
                "page_text_length": len(content),
                "html_length": len(html),
                "sample_content": content[:3000],
                "bid_ask_samples": bid_ask_pattern[:10],
                "screenshot_path": screenshot_path
            }
    
    from app.services.stocknear_service import run_scraper
    result = await run_scraper(scrape_sync)
    return result


@router.delete("/cache")
async def clear_all_cache(db: AsyncSession = Depends(get_db)):
    """Clear all StockNear cache entries."""
    from sqlalchemy import delete
    from app.models import StockNearCache
    
    result = await db.execute(delete(StockNearCache))
    await db.commit()
    return {"deleted": result.rowcount}


@router.delete("/cache/{symbol}")
async def clear_symbol_cache(symbol: str, db: AsyncSession = Depends(get_db)):
    """Clear cache entries for a specific symbol."""
    from sqlalchemy import delete
    from app.models import StockNearCache
    
    result = await db.execute(
        delete(StockNearCache).where(StockNearCache.symbol == symbol.upper())
    )
    await db.commit()
    return {"symbol": symbol.upper(), "deleted": result.rowcount}


@router.get("/quote-api/{symbol}")
async def debug_quote_api(
    symbol: str,
    expiration: str,
    strike: float,
    option_type: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint to test the new direct API quote method.
    This is much faster than browser scraping.
    
    Example: /api/debug/quote-api/AAPL?expiration=Jan%2030,%202026&strike=250&option_type=CALL
    """
    from app.stocknear import StockNearScraper
    import asyncio
    
    def fetch_sync():
        with StockNearScraper() as scraper:
            quote = scraper.get_contract_quote_via_api(
                symbol=symbol,
                expiration=expiration,
                strike=strike,
                option_type=option_type
            )
            return {
                "contract_id": quote.contract_id,
                "symbol": quote.symbol,
                "strike": quote.strike,
                "option_type": quote.option_type,
                "expiration": quote.expiration,
                "bid": quote.bid,
                "ask": quote.ask,
                "mid": quote.mid,
                "last": quote.last,
                "volume": quote.volume,
                "open_interest": quote.open_interest,
                "implied_volatility": quote.implied_volatility,
                "delta": quote.delta,
                "gamma": quote.gamma,
                "theta": quote.theta,
                "vega": quote.vega,
                "raw_content_length": len(quote.raw_content),
                "raw_content_preview": quote.raw_content[:500] if quote.raw_content else None
            }
    
    from app.services.stocknear_service import run_scraper
    result = await run_scraper(fetch_sync)
    return result


@router.get("/strikes/{symbol}")
async def debug_strikes(symbol: str, raw_html: bool = False):
    """
    Debug endpoint to test get_available_strikes() method.
    Returns available strike prices and expirations for a symbol.
    """
    from app.stocknear import StockNearScraper
    import asyncio
    
    def fetch_sync():
        with StockNearScraper() as scraper:
            result = scraper.get_available_strikes(symbol, return_raw_html=raw_html)
            
            return {
                "symbol": symbol.upper(),
                "current_price": result.get("current_price"),
                "expiration_count": len(result.get("expirations", [])),
                "expirations": result.get("expirations", [])[:15],
                "strike_count": len(result.get("strikes", [])),
                "strikes_sample": result.get("strikes", [])[:30],  # First 30 strikes
                "debug_info": result.get("_debug", {})
            }
    
    from app.services.stocknear_service import run_scraper
    result = await run_scraper(fetch_sync)
    return result


@router.get("/assignment-calc")
async def debug_assignment_calc(
    symbol: str,
    strike: float,
    option_type: str,
    days: int,
    current_price: float,
    iv: Optional[float] = None,  # Implied volatility as decimal (e.g., 0.80 for 80%)
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint to trace assignment probability calculation step by step.
    
    This helps verify that _estimate_delta() and calculate_price_at_delta() are consistent.
    
    Example: /api/debug/assignment-calc?symbol=CSIQ&strike=20&option_type=PUT&days=168&current_price=19.77
    
    Expected result: At current_price close to strike, probability should be ~50%.
    The price_at_50pct should be close to strike for reasonable IV levels.
    """
    import math
    from app.services.risk_analysis import (
        _estimate_delta,
        calculate_price_at_delta,
        _calculate_d1_d2,
        _norm_cdf,
        DEFAULT_VOLATILITY,
        DEFAULT_RISK_FREE_RATE,
        CALENDAR_DAYS_PER_YEAR,
    )
    from app.services.stocknear_service import get_options_overview
    
    option_type = option_type.upper()
    symbol = symbol.upper()
    
    # Get IV from StockNear if not provided
    iv_source = "provided"
    volatility = iv
    
    if volatility is None:
        try:
            options_data = await get_options_overview(db, symbol)
            if options_data and options_data.implied_volatility:
                volatility = options_data.implied_volatility
                iv_source = f"stocknear ({volatility:.1%})"
            else:
                volatility = DEFAULT_VOLATILITY
                iv_source = f"default ({DEFAULT_VOLATILITY:.1%})"
        except Exception as e:
            volatility = DEFAULT_VOLATILITY
            iv_source = f"default (error: {str(e)[:50]})"
    
    # Step-by-step calculation
    T = days / CALENDAR_DAYS_PER_YEAR
    sqrt_T = math.sqrt(T)
    
    # Calculate d1 and d2 at current price
    d1, d2 = _calculate_d1_d2(current_price, strike, T, DEFAULT_RISK_FREE_RATE, volatility)
    
    # Calculate assignment probability using _estimate_delta
    assignment_prob = _estimate_delta(option_type, strike, current_price, days, volatility=volatility)
    
    # Calculate price at 50% assignment
    price_at_50pct = calculate_price_at_delta(option_type, strike, days, target_delta=0.5, volatility=volatility)
    
    # Manual verification of the formulas
    # For PUT: P(ITM) = N(-d2)
    # For CALL: P(ITM) = N(d2)
    if option_type == "PUT":
        manual_prob = _norm_cdf(-d2)
    else:
        manual_prob = _norm_cdf(d2)
    
    # Verify price_at_50pct by calculating d2 at that price
    d1_at_50: Optional[float] = None
    d2_at_50: Optional[float] = None
    prob_at_50_price: Optional[float] = None
    if price_at_50pct:
        d1_at_50, d2_at_50 = _calculate_d1_d2(price_at_50pct, strike, T, DEFAULT_RISK_FREE_RATE, volatility)
        if option_type == "PUT":
            prob_at_50_price = _norm_cdf(-d2_at_50)
        else:
            prob_at_50_price = _norm_cdf(d2_at_50)
    
    # Expected price at 50% for PUT: when d2 = 0
    # S = K * exp((σ²/2 - r) * T)
    # This is the theoretical 50% point
    drift_term = (0.5 * volatility ** 2 - DEFAULT_RISK_FREE_RATE) * T
    theoretical_50pct_put = strike * math.exp(drift_term)
    
    # For CALL: when d2 = 0, same formula
    theoretical_50pct_call = theoretical_50pct_put
    
    return {
        "input": {
            "symbol": symbol,
            "strike": strike,
            "option_type": option_type,
            "days_to_expiry": days,
            "current_price": current_price,
            "iv_used": round(volatility * 100, 2),
            "iv_source": iv_source,
        },
        "time_params": {
            "T_years": round(T, 6),
            "sqrt_T": round(sqrt_T, 6),
            "risk_free_rate": DEFAULT_RISK_FREE_RATE,
        },
        "d1_d2_at_current_price": {
            "d1": round(d1, 6),
            "d2": round(d2, 6),
            "N(d2)": round(_norm_cdf(d2), 6),
            "N(-d2)": round(_norm_cdf(-d2), 6),
        },
        "assignment_probability": {
            "from_estimate_delta": round(assignment_prob * 100, 2),
            "manual_calc": round(manual_prob * 100, 2),
            "match": abs(assignment_prob - manual_prob) < 0.0001,
        },
        "price_at_50pct": {
            "calculated": round(price_at_50pct, 4) if price_at_50pct else None,
            "theoretical": round(theoretical_50pct_put, 4),
            "match": abs(price_at_50pct - theoretical_50pct_put) < 0.01 if price_at_50pct else None,
        },
        "verification_at_50pct_price": {
            "d1_at_50pct": round(d1_at_50, 6) if d1_at_50 else None,
            "d2_at_50pct": round(d2_at_50, 6) if d2_at_50 else None,
            "prob_at_50pct_price": round(prob_at_50_price * 100, 2) if prob_at_50_price else None,
            "should_be_50": prob_at_50_price is not None and abs(prob_at_50_price - 0.5) < 0.01,
        },
        "interpretation": {
            "current_vs_strike": "ITM" if (option_type == "PUT" and current_price < strike) or (option_type == "CALL" and current_price > strike) else "OTM",
            "distance_to_strike_pct": round(abs(current_price - strike) / strike * 100, 2),
            "high_iv_effect": "With high IV, the 50% assignment price can be above strike for PUTs due to drift" if volatility > 0.5 and option_type == "PUT" and price_at_50pct is not None and price_at_50pct > strike else None,
        },
        "math_explanation": {
            "formula_d2": "d2 = (ln(S/K) + (r - σ²/2)T) / (σ√T)",
            "for_put_50pct": "When N(-d2) = 0.5, d2 = 0, so ln(S/K) = (σ²/2 - r)T",
            "high_iv_insight": f"With σ={volatility:.0%}, drift term = (σ²/2 - r)T = ({0.5*volatility**2:.4f} - {DEFAULT_RISK_FREE_RATE})×{T:.4f} = {drift_term:.4f}",
            "result": f"S = K × exp({drift_term:.4f}) = {strike} × {math.exp(drift_term):.4f} = {strike * math.exp(drift_term):.2f}"
        }
    }

