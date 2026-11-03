-- Hourly shape for one day. Columns: h, spans, ips, apis. Cheap; used for the traffic-shape check.
-- Placeholders: {{base}}, {{day}}
SELECT date_trunc('hour', start_time_millis_ts) h, count(*) spans, approx_distinct(ip_address) ips, approx_distinct(api_name) apis
FROM span_event_view WHERE {{base}} AND {{day}} GROUP BY 1 ORDER BY 1
