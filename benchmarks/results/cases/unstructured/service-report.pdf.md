Acme Corporation — Service Level Report
# Service Level Report
1. Availability Availability is measured per calendar month at the public API gateway. Planned maintenance windows announced at least 72 hours ahead are excluded.
# Service
# Target
# October
# November
# December
# API gateway
99.95%
99.97%
99.99%
99.96%
# Web console
99.90%
99.93%
99.88%
99.95%
Batch exports
99.50%
99.71%
99.64%
99.80%
2. Incidents The quarter had three incidents. None breached the monthly target.
# Date
# Service
# Duration
# Cause
2025-10-08
# Web console
# 14 min
# Expired certificate
2025-11-19
# Web console
# 52 min
# Database failover
2025-12-02
Batch exports
# 31 min
# Queue backlog
Page 1
Acme Corporation — Service Level Report
3. Analysis
# 3.1 Review round 1
Root cause analysis. The certificate that expired on 8 October had been issued manually and was not covered by the automatic renewal job. All certificates were moved to automatic renewal on 15 October.
The failover of 19 November was triggered by a storage controller fault. Replication lag at the moment of failover was 4 seconds, which is within the recovery point objective of 30 seconds.
The export backlog of 2 December followed an unusually large customer job. A per-tenant concurrency limit now prevents one job from occupying all workers.
Preventive work planned for the next quarter includes a second gateway region, quarterly failover drills and a load test at three times the December peak.
# 3.2 Review round 2
Root cause analysis. The certificate that expired on 8 October had been issued manually and was not covered by the automatic renewal job. All certificates were moved to automatic renewal on 15 October.
The failover of 19 November was triggered by a storage controller fault. Replication lag at the moment of failover was 4 seconds, which is within the recovery point objective of 30 seconds.
The export backlog of 2 December followed an unusually large customer job. A per-tenant concurrency limit now prevents one job from occupying all workers.
Preventive work planned for the next quarter includes a second gateway region, quarterly failover drills and a load test at three times the December peak.
3.3 Review round 3
Root cause analysis. The certificate that expired on 8 October had been issued manually and was not covered by the automatic renewal job. All certificates were moved to automatic renewal on
# 15 October.
The failover of 19 November was triggered by a storage controller fault. Replication lag at the moment of failover was 4 seconds, which is within the recovery point objective of 30 seconds.
The export backlog of 2 December followed an unusually large customer job. A per-tenant concurrency limit now prevents one job from occupying all workers.
Preventive work planned for the next quarter includes a second gateway region, quarterly failover drills and a load test at three times the December peak.
# 3.4 Review round 4
Root cause analysis. The certificate that expired on 8 October had been issued manually and was not covered by the automatic renewal job. All certificates were moved to automatic renewal on 15 October.
The failover of 19 November was triggered by a storage controller fault. Replication lag at the moment of failover was 4 seconds, which is within the recovery point objective of 30 seconds.
The export backlog of 2 December followed an unusually large customer job. A per-tenant concurrency limit now prevents one job from occupying all workers.
Preventive work planned for the next quarter includes a second gateway region, quarterly failover drills and a load test at three times the December peak.
# 3.5 Review round 5
Root cause analysis. The certificate that expired on 8 October had been issued manually and was not covered by the automatic renewal job. All certificates were moved to automatic renewal on 15 October.
The failover of 19 November was triggered by a storage controller fault. Replication lag at the moment of failover was 4 seconds, which is within the recovery point objective of 30 seconds.
Page 2
Acme Corporation — Service Level Report
The export backlog of 2 December followed an unusually large customer job. A per-tenant concurrency limit now prevents one job from occupying all workers.
Preventive work planned for the next quarter includes a second gateway region, quarterly failover drills and a load test at three times the December peak.
# 3.6 Review round 6
Root cause analysis. The certificate that expired on 8 October had been issued manually and was not covered by the automatic renewal job. All certificates were moved to automatic renewal on 15 October.
The failover of 19 November was triggered by a storage controller fault. Replication lag at the moment of failover was 4 seconds, which is within the recovery point objective of 30 seconds.
The export backlog of 2 December followed an unusually large customer job. A per-tenant concurrency limit now prevents one job from occupying all workers.
Preventive work planned for the next quarter includes a second gateway region, quarterly failover drills and a load test at three times the December peak.
# 3.7 Review round 7
Root cause analysis. The certificate that expired on 8 October had been issued manually and was not covered by the automatic renewal job. All certificates were moved to automatic renewal on 15 October.
The failover of 19 November was triggered by a storage controller fault. Replication lag at the moment of failover was 4 seconds, which is within the recovery point objective of 30 seconds.
The export backlog of 2 December followed an unusually large customer job. A per-tenant concurrency limit now prevents one job from occupying all workers.
Preventive work planned for the next quarter includes a second gateway region, quarterly failover drills and a load test at three times the December peak.
# 3.8 Review round 8
Root cause analysis. The certificate that expired on 8 October had been issued manually and was not covered by the automatic renewal job. All certificates were moved to automatic renewal on 15 October.
The failover of 19 November was triggered by a storage controller fault. Replication lag at the moment of failover was 4 seconds, which is within the recovery point objective of 30 seconds.
The export backlog of 2 December followed an unusually large customer job. A per-tenant concurrency limit now prevents one job from occupying all workers.
Preventive work planned for the next quarter includes a second gateway region, quarterly failover drills and a load test at three times the December peak.
# 3.9 Review round 9
Root cause analysis. The certificate that expired on 8 October had been issued manually and was not covered by the automatic renewal job. All certificates were moved to automatic renewal on 15 October.
The failover of 19 November was triggered by a storage controller fault. Replication lag at the moment of failover was 4 seconds, which is within the recovery point objective of 30 seconds.
The export backlog of 2 December followed an unusually large customer job. A per-tenant concurrency limit now prevents one job from occupying all workers.
Preventive work planned for the next quarter includes a second gateway region, quarterly failover drills and a load test at three times the December peak.
Page 3