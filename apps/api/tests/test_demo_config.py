import pytest
from app.core.config import Settings


def test_shared_startup_rejects_default_secrets():
    config=Settings(_env_file=None,semantic_env='production',jwt_secret_key='replace-me')
    with pytest.raises(ValueError,match='jwt_secret_key'):
        config.validate_shared_secrets()


@pytest.mark.parametrize('name',['jwt_secret_key','encryption_master_key','agent_shared_token'])
def test_shared_startup_checks_every_secret(name):
    values={key:'c35f4b9aa96b41029fa0db1e552039293147881afa624c40' for key in ('jwt_secret_key','encryption_master_key','agent_shared_token')}
    values[name]='replace-me'*8
    with pytest.raises(ValueError,match=name):
        Settings(_env_file=None,semantic_env='shared',**values).validate_shared_secrets()


def test_development_configuration_remains_available():
    Settings(_env_file=None,semantic_env='development').validate_shared_secrets()
