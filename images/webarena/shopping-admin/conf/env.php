<?php
return [
    'cache_types' => [
        'compiled_config' => 1,
        'config' => 1,
        'layout' => 1,
        'block_html' => 1,
        'collections' => 1,
        'reflection' => 1,
        'db_ddl' => 1,
        'eav' => 1,
        'customer_notification' => 1,
        'config_integration' => 1,
        'config_integration_api' => 1,
        'full_page' => 1,
        'config_webservice' => 1,
        'translate' => 1
    ],
    'backend' => [
        'frontName' => 'admin'
    ],
    'cache' => [
        'graphql' => [
            'id_salt' => 'iXYcpZnFO8PNtq5NEwUduuPiNpTLhuKL'
        ],
        'frontend' => [
            'default' => [
                'id_prefix' => 'a6b_',
                'backend' => 'Magento\\Framework\\Cache\\Backend\\Redis',
                'backend_options' => [
                    'server' => '127.0.0.1',
                    'database' => '0',
                    'port' => '6379',
                    'password' => '',
                    'compress_data' => '1',
                    'compression_lib' => ''
                ]
            ],
            'page_cache' => [
                'id_prefix' => 'a6b_',
                'backend' => 'Magento\\Framework\\Cache\\Backend\\Redis',
                'backend_options' => [
                    'server' => '127.0.0.1',
                    'database' => '1',
                    'port' => '6379',
                    'password' => '',
                    'compress_data' => '0',
                    'compression_lib' => ''
                ]
            ]
        ],
        'allow_parallel_generation' => false
    ],
    'remote_storage' => [
        'driver' => 'file'
    ],
    'queue' => [
        'consumers_wait_for_messages' => 1
    ],
    'crypt' => [
        'key' => '67efb5f1128ec7933c3df0d5da585ab4'
    ],
    'db' => [
        'table_prefix' => '',
        'connection' => [
            'default' => [
                'host' => '127.0.0.1',
                'dbname' => 'magentodb',
                'username' => 'magentouser',
                'password' => 'MyPassword',
                'model' => 'mysql4',
                'engine' => 'innodb',
                'initStatements' => 'SET NAMES utf8;',
                'active' => '1',
                'driver_options' => [
                    1014 => false
                ]
            ]
        ]
    ],
    'resource' => [
        'default_setup' => [
            'connection' => 'default'
        ]
    ],
    'x-frame-options' => 'SAMEORIGIN',
    'MAGE_MODE' => 'default',
    'session' => [
        'save' => 'files'
    ],
    'lock' => [
        'provider' => 'db'
    ],
    'directories' => [
        'document_root_is_pub' => true
    ],
    // Serve whatever HTTP host the request arrived on. The restored dataset's
    // core_config_data still names CMU's original deployment, and Magento
    // answers any other host with a 302 to it — the image demanded to be
    // reached as metis.lti.cs.cmu.edu or not at all, which is why the smoke
    // gate could only ever reach it by spoofing a Host header. Deployment
    // config outranks the database, so this overrides the baked host without
    // rewriting the data. We run no name-based virtual hosts, so there is no
    // hostname for this image to have an opinion about.
    'downloadable_domains' => [
        $_SERVER['HTTP_HOST'] ?? 'localhost:7780'
    ],
    'system' => [
        'default' => [
            'catalog' => [
                'search' => [
                    'engine' => 'elasticsearch7',
                    'elasticsearch7_server_hostname' => '127.0.0.1',
                    'elasticsearch7_server_port' => '9200'
                ]
            ],
            'web' => [
                // Generated absolute URLs follow the caller's own host, so a
                // page fetched over localhost, an IP or a container name links
                // back to the same place it was fetched from. The fallback
                // covers CLI bootstraps (bin/magento, cron), which have no
                // request and never emit links.
                'unsecure' => [
                    'base_url' => 'http://' . ($_SERVER['HTTP_HOST'] ?? 'localhost:7780') . '/'
                ],
                'secure' => [
                    'base_url' => 'http://' . ($_SERVER['HTTP_HOST'] ?? 'localhost:7780') . '/'
                ],
                // ...and never bounce a request to base_url for arriving under
                // a different name. This is the setting that emitted the 302.
                'url' => [
                    'redirect_to_base' => 0
                ]
            ]
        ]
    ],
    'install' => [
        'date' => 'Wed, 19 Apr 2023 15:45:39 +0000'
    ]
];
