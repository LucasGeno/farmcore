"""App `activity`: every action on the farm, newest first, with columns the viewer picks.

Rows come from `core.activity.activity_rows` (the same grouping as Today's Recent activity); the
app reads only core models through the request's `ScopedRepository`, so it imports no other app.
"""
