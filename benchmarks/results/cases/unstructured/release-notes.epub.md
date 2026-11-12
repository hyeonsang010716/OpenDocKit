# Release notes 2.4
# Added
--json output for the command line tool
Retry with backoff for uploads
# Fixed
<table><thead><tr><th>Issue</th><th>Area</th><th>Fix</th></tr></thead><tbody><tr><td>#812</td><td>Export</td><td>Dates lost their time zone</td></tr><tr><td>#840</td><td>Login</td><td>Session expired during upload</td></tr></tbody></table>
client.upload(path, retries=3)