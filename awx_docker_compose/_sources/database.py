DATABASES = {
    'default': {
        'ATOMIC_REQUESTS': True,
        'ENGINE': 'awx.main.db.profiled_pg',
        'NAME': "awx",
        'USER': "awx",
        'PASSWORD': "vikoqBQDQLfuNBaBStzQ",
        'HOST': "postgres",
        'PORT': "5432",
    }
}
