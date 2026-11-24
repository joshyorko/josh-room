# SessionFence

TLC reached 72 states and 120 steps. The chart draws 7 states and 10 arrows.
The chart does not draw 54 steps that stay in one chart state.

## NotYetDispatched breaks
The shortest path has 2 steps:
0. Ready for a bound request
1. Explorer submits a bound request: Bound request pending
2. Receiver checks immediately before dispatch: Current request dispatched

## Rules that hold
NoDeadlock
