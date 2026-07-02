# -*- coding: utf-8 -*-
"""Adaptador GitHub: historial de commits de un repositorio (Fase 5).

Servicio Python **puro**: no depende del ORM de Odoo ni escribe en la base.
Recibe un token (opcional, para repos privados) y devuelve datos normalizados.
Los modelos lo consumen por inyección, lo que habilita el testeo con mocks.
"""
from github import Auth, Github


class GithubApiService:
    """Operaciones de lectura sobre la API de GitHub vía PyGithub."""

    def __init__(self, token=None, client=None):
        """Inicializa el cliente PyGithub.

        Args:
            token (str, optional): Personal Access Token. Sin token se opera de
                forma anónima (solo repos públicos, con rate limit más bajo).
            client (github.Github, optional): cliente ya construido (inyección
                para tests). Si se pasa, se ignora ``token``.
        """
        if client is not None:
            self._client = client
        elif token:
            self._client = Github(auth=Auth.Token(token))
        else:
            self._client = Github()

    @staticmethod
    def parse_repo_slug(github_url, organization=None):
        """Deriva ``org/repo`` de una URL de GitHub.

        Args:
            github_url (str): ej. ``https://github.com/odoo/odoo`` o ``.git``.
            organization (str, optional): organización explícita (tiene prioridad
                para el dueño si la URL no la trae limpia).

        Returns:
            str: slug ``owner/repo``.

        Raises:
            ValueError: si no se puede derivar el slug.
        """
        url = (github_url or "").strip()
        if not url:
            raise ValueError("Falta la URL del repositorio de GitHub.")
        slug = url.split("github.com")[-1]
        slug = slug.lstrip(":/").removesuffix(".git").strip("/")
        parts = [p for p in slug.split("/") if p]
        if len(parts) < 2:
            raise ValueError("No se pudo derivar 'owner/repo' de: %s" % github_url)
        owner = organization or parts[0]
        return "%s/%s" % (owner, parts[1])

    def list_commits(self, repo_slug, branch=None, limit=50):
        """Lista los commits más recientes de una rama.

        Args:
            repo_slug (str): ``owner/repo``.
            branch (str, optional): rama; usa la rama por defecto si falta.
            limit (int): máximo de commits a traer.

        Returns:
            list[dict]: commits normalizados (ver :meth:`_normalize_commit`).
        """
        repo = self._client.get_repo(repo_slug)
        kwargs = {"sha": branch} if branch else {}
        commits = repo.get_commits(**kwargs)
        result = []
        for index, commit in enumerate(commits):
            if index >= limit:
                break
            result.append(self._normalize_commit(commit, branch))
        return result

    def get_latest_commit(self, repo_slug, branch=None):
        """Devuelve el último commit de la rama, o None si no hay."""
        commits = self.list_commits(repo_slug, branch=branch, limit=1)
        return commits[0] if commits else None

    @staticmethod
    def _normalize_commit(commit, branch):
        """Convierte un objeto Commit de PyGithub a un dict estable."""
        data = commit.commit
        author = getattr(data, "author", None)
        return {
            "commit_hash": commit.sha,
            "message": (data.message or "").strip(),
            "author": getattr(author, "name", None) or "",
            "commit_date": getattr(author, "date", None),
            "branch": branch or "",
        }
