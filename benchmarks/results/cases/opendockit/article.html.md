# Deploying with blue-green releases

A blue-green release keeps two identical environments. Traffic moves from blue to green in one switch, and moves back just as quickly if something is wrong.

## Checklist

1. Deploy the new version to green.
2. Run smoke tests against green.
  - Login
  - Checkout
3. Switch the load balancer.

## Cost and risk

| Strategy | Cost Compute | Cost Operations | Rollback time |
| --- | --- | --- | --- |
| Blue-green | 2× | Low | Seconds |
| Rolling | 1.1× | Medium | Minutes |

## Switch command

```
lb set-backend --pool green --drain 30s
lb status --wait healthy
```

Expected error budget burn during a switch is $\frac{t·e}{T}$, where t is the switch time.

© 2026 Example Cloud. All rights reserved.