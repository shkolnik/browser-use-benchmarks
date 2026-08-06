<?php
// Osclass configuration, byte-for-byte upstream's except for DB_HOST.
//
// ⚠️ DEVIATION 3 (one appliance, not two containers): upstream is a compose
// project whose web service reaches the database at host `db`. Every image in
// this fleet is a single self-contained appliance with one healthcheck, so the
// server runs beside PHP here and the host becomes `localhost` — which mysqli
// resolves to the unix socket, matching the `root@localhost` grant the restore
// stage creates. The reset controller shells out to `mysql -h localhost` and
// takes the same path.
//
// WEB_PATH stays a runtime lookup, exactly as upstream had it: the base URL is
// injected by the CLASSIFIEDS environment variable, never baked into the image.

// MySql database host
define('DB_HOST', 'localhost');

// MySql database username
define('DB_USER', 'root');

// MySql database password
define('DB_PASSWORD', 'password');

// MySql database name
define('DB_NAME', 'osclass');

// MySql database table prefix
define('DB_TABLE_PREFIX', 'oc_');

// Relative web url
define('REL_WEB_URL', '/');

// Web address - modify here for SSL version of site
define('WEB_PATH', getenv("CLASSIFIEDS"));
