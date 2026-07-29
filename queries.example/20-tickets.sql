-- Confirmed public-transport ticket bookings.
SELECT ifNull(toString(cloud_type), 'UNATTRIBUTED') AS cloud,
       count(DISTINCT id)                           AS n
FROM <tickets_table> FINAL
WHERE status = 'CONFIRMED'
  AND toDate(created_at + toIntervalSecond(330*60)) = toDate('{day}')
GROUP BY cloud
FORMAT TabSeparated
