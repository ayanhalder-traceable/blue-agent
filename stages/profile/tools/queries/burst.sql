-- Per-API minute profile for one day. Columns: api_name, minutes, peak, median, peak_minute
-- minutes = minutes with at least one span; peak = busiest minute; median = median over active minutes. Burstiness = peak / median.
-- Placeholders: {{base}}, {{day}}
SELECT api_name, count(*) minutes, max(c) peak, approx_percentile(c, 0.5) median, max_by(m, c) peak_minute
FROM (SELECT coalesce(api_name,'<NULL>') api_name, date_trunc('minute', start_time_millis_ts) m, count(*) c FROM span_event_view WHERE {{base}} AND {{day}} GROUP BY 1,2)
GROUP BY 1
