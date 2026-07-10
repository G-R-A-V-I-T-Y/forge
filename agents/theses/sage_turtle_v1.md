# sage_turtle -- Thesis v1: Event & Unlock Positioning

## Edge Hypothesis

Scheduled supply and macro events are public, dated, and repeatedly under-anticipated by the market until they are imminent. Token unlocks release a known quantity of new supply to holders (often early investors/team with a low cost basis and a high propensity to sell) at a known timestamp; the market chronically underprices the sell pressure until the unlock is within days, then overcorrects. Macro events (FOMC, CPI) do not move any single asset's supply, but they reset the funding/leverage backdrop for the entire book in ways theses cannot see coming. This agent does not predict price from price -- it predicts price from the calendar: what is scheduled, how large is it relative to float, and how has the market historically reacted to this specific event type.

## Entry Decision

- Confident entry (confidence >= 0.70): full position size at standard parameters
- Moderate entry (confidence 0.50-0.70): scale position size by confidence factor
- No entry (confidence < 0.50): firm rule -- wait, but log to the watchlist if an event is within 10 days

## Position Parameters

- Direction: Short into large unlocks (dilution); long only in the rare case of a documented buyback/burn event with equivalent evidence structure inverted.
- Leverage: 3x
- Position size: 10% of account per trade
- Stop loss: 3.0% from entry
- Take profit: 6.0% from entry
- Max hold time: through the event plus 24 hours, then exit regardless of P&L
