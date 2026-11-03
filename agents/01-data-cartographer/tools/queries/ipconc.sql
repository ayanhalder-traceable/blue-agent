-- Top-1 caller share per api_name. Columns: api_name, spans, top1, top1_ip
SELECT api_name, sum(c) spans, max(c) top1, max_by(ip_address, c) top1_ip
FROM (SELECT coalesce(api_name,'<NULL>') api_name, ip_address, count(*) c FROM span_event_view WHERE {{base}} AND {{day}} GROUP BY 1,2)
GROUP BY 1
