-- One row per api_name for one full real day. Run on the full day (not a sample).
-- Placeholders: {{base}} = customer_id and environment filter, {{day}} = start_time_millis_ts range for one real day.
SELECT coalesce(api_name,'<NULL>') api_name, count(*) spans,
  approx_distinct(ip_address) ips, approx_distinct(user_identifier) users,
  count_if(try_cast(status_code AS integer) BETWEEN 400 AND 499) s4xx,
  count_if(try_cast(status_code AS integer) >= 500) s5xx,
  count_if(try_cast(status_code AS integer) = 404) s404,
  count_if(try_cast(status_code AS integer) IN (401,403)) s401_3,
  count_if(element_at(request_headers,'authorization') IS NOT NULL) has_auth,
  count_if(cardinality(request_cookies) > 0) has_cookie,
  count_if(length(request_body) > 0) has_reqbody,
  count_if(length(response_body) > 0) has_respbody,
  count_if(length(user_identifier) > 0) has_uid,
  count_if(length(ip_address) > 0) has_ip,
  count_if(length(user_country) > 0) has_country,
  count_if(ip_is_bot OR ip_is_scanner) bot_scan,
  count_if(is_ip_external) ext_ip,
  count_if(api_is_external AND is_ip_external) ext_both,
  approx_distinct(user_agent) uas, approx_distinct(http_url_path) paths
FROM span_event_view WHERE {{base}} AND {{day}} GROUP BY 1
