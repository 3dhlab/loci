import unittest
from disposable_targets import require_postgres_target, require_redis_target


class DisposableTargetTests(unittest.TestCase):
    def test_loopback_named_test_databases(self):
        require_postgres_target("postgresql+psycopg://test_role:secret@127.0.0.1:5432/semantic_ci", environ={})
        require_postgres_target("postgresql://test_role@localhost/semantic_ci_migration", migration=True, environ={})
        require_redis_target("redis://127.0.0.1:6379/15", environ={})

    def test_local_container_services_require_explicit_opt_in(self):
        url = "postgresql+psycopg://generated_role:secret@postgres:5432/loci_correction_qa"
        with self.assertRaises(ValueError):
            require_postgres_target(url, environ={})
        require_postgres_target(url, environ={"CI_DISPOSABLE_SERVICES": "true"})
        require_redis_target("redis://redis:6379/15", environ={"CI_DISPOSABLE_SERVICES": "true"})

    def test_remote_or_production_targets_are_rejected(self):
        for host in ["production.example", "postgres.example", "192.168.1.10"]:
            with self.assertRaises(ValueError):
                require_postgres_target(f"postgresql://test_role@{host}/semantic_ci", environ={"CI_DISPOSABLE_SERVICES": "true"})
        with self.assertRaises(ValueError):
            require_postgres_target("postgresql://test_role@localhost/semantic_ci", environ={"SEMANTIC_ENV": "production"})

    def test_migration_requires_its_own_database(self):
        for database in ["postgres", "loci_correction_qa", "semantic_ci", "production"]:
            with self.assertRaises(ValueError):
                require_postgres_target(f"postgresql://test_role@localhost/{database}", migration=True, environ={})

    def test_missing_malformed_and_extra_connection_options_are_rejected(self):
        for url in ["", "postgresql://test_role@localhost:bad/semantic_ci", "postgresql://test_role@localhost/semantic_ci?host=remote.example", "postgresql://localhost/semantic_ci", "postgresql://test_role@localhost/semantic_ci#extra"]:
            with self.assertRaises(ValueError):
                require_postgres_target(url, environ={})

    def test_redis_requires_database_15_and_local_scope(self):
        for url in ["redis://localhost/0", "redis://remote.example/15", "redis://redis/15", "redis://localhost/15?db=0"]:
            with self.assertRaises(ValueError):
                require_redis_target(url, environ={})

    def test_errors_do_not_echo_credentials(self):
        with self.assertRaises(ValueError) as error:
            require_postgres_target("postgresql://role:sensitive-password@remote.example/production", environ={})
        self.assertNotIn("sensitive-password", str(error.exception))


if __name__ == "__main__":
    unittest.main()
