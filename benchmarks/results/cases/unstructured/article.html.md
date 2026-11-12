# Deploying with blue-green releases
A blue-green release keeps two identical environments. Traffic moves from blue to green in one switch, and moves back just as quickly if something is wrong.
This paragraph is hidden and is not part of the article.
Tracking pixel placeholder — also hidden.
# Checklist
Deploy the new version to green.
Run smoke tests against green.
Login
Checkout
Switch the load balancer.
# Cost and risk
<table><tr><th>Strategy</th><th>Cost</th><th>Rollback time</th></tr><tr><th>Compute</th><th>Operations</th></tr><tr><td>Blue-green</td><td>2×</td><td>Low</td><td>Seconds</td></tr><tr><td>Rolling</td><td>1.1×</td><td>Medium</td><td>Minutes</td></tr></table>
# Switch command
lb set-backend --pool green --drain 30s
lb status --wait healthy
Expected error budget burn during a switch is , where t is the switch time.