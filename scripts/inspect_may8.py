"""Inspect observation rows for the suspicious 2026-05-08 universal cold tail."""

from polymarket_weather.db import with_conn


with with_conn() as conn:
    with conn.cursor() as cur:
        print("--- observations.observed_max_f for 2026-05-08 (resolution sources) ---")
        cur.execute(
            """
            SELECT s.slug, o.source, o.obs_date, o.observed_max_f,
                   o.finalized, o.ingested_at
            FROM observations o
            JOIN stations s USING (station_id)
            WHERE o.obs_date = DATE '2026-05-08'
            ORDER BY s.slug, o.source
            """
        )
        for r in cur.fetchall():
            print(
                f"  {r[0]:<14} {r[1]:<28} {r[2]} "
                f"observed_max_f={r[3]}  finalized={r[4]}  ingested_at={r[5]}"
            )
        print()
        print("--- observations for 2026-05-06 .. 2026-05-10 (NYC) ---")
        cur.execute(
            """
            SELECT o.obs_date, o.source, o.observed_max_f, o.finalized
            FROM observations o
            JOIN stations s USING (station_id)
            WHERE s.slug = 'nyc'
              AND o.obs_date BETWEEN DATE '2026-05-06' AND DATE '2026-05-10'
            ORDER BY o.obs_date, o.source
            """
        )
        for r in cur.fetchall():
            print(
                f"  {r[0]} {r[1]:<28} observed_max_f={r[2]}  finalized={r[3]}"
            )
        print()
        print("--- pm_buckets resolved for 2026-05-08 events ---")
        cur.execute(
            """
            SELECT e.event_slug, e.target_date, b.bucket_label, b.realised
            FROM pm_events e
            JOIN pm_buckets b USING (event_slug)
            WHERE e.target_date = DATE '2026-05-08'
              AND b.realised = TRUE
            ORDER BY e.event_slug
            """
        )
        for r in cur.fetchall():
            print(f"  {r[0]:<55}  realised: {r[2]}")
