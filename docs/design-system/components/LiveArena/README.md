The live match panel for spectators: team names in `t1` blue and `t2` red around the big score and the clock, the win-probability bar (with a trend line once shots arrive), both racks, then per-player stats and emoji cheering.

Shots now come only from the referee; spectators can only cheer, and a couple that is playing is told to put the phone down. Rendered client-side in `live.html` from `/api/live/<id>`, polled every 2 s.

**Roast:** the left rack is team 2's cups and the right rack team 1's. The probability is recomputed with 1,000 simulations on every poll from every phone.
