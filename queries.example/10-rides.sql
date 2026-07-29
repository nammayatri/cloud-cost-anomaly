-- Completed ride-hailing trips, dated in the business timezone.
-- Replace the table/columns to match your schema.
SELECT ifNull(toString(cloud_type), 'UNATTRIBUTED') AS cloud,
       count(DISTINCT id)                           AS n
FROM <rides_table> FINAL
WHERE status = 'COMPLETED'
  AND toDate(created_at + toIntervalSecond(330*60)) = toDate('{day}')
GROUP BY cloud
FORMAT TabSeparated
