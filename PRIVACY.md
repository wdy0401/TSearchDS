# Privacy and network behavior

The application does not maintain a saved search-history database. This is not
a promise of anonymity or that no traces exist on the computer or network.

- Queries are sent to selected search sites. They and network/proxy operators
  may observe or record queries, IP addresses and requests.
- Tracker enrichment sends resource hashes to trackers. eD2k/Kad searches contact
  servers or peers; UDP traffic may use a direct route depending on configuration.
- Subscription refresh contacts the subscription provider. Node tests contact
  test endpoints through selected nodes. A proxy does not guarantee anonymity.
- Subscriptions and node credentials are stored locally in plaintext configuration
  files. Runtime proxy configuration also contains credentials. Do not share these.
- Application diagnostics, mihomo logs, crash reports, OS caches and clipboard
  contents can contain sensitive information. Review and redact before reporting issues.
- Settings, layouts and imported nodes may persist. Deleting the application alone
  does not necessarily delete its user-data directory.

There is no project-operated analytics endpoint in the reviewed source. This
does not describe the retention practices of third-party services.

Use the application's data-directory menu to locate local files. Close the
application before removing settings, logs or cached proxy data you no longer need.
