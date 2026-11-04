"""Repositories for the shared tenant tables. Repositories never commit; the caller's unit of work does.

`TenantScope` is the only way tenant-owned rows are queried, so a missing client/project filter cannot happen.
"""
from dataclasses import dataclass
from typing import Generic, Optional, Sequence, TypeVar

from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from ats_common.db.tables import Client, Project, ProjectMember, User, utcnow

M = TypeVar("M", bound=SQLModel)


@dataclass(frozen=True)
class TenantScope:
    client_id: int
    project_id: int

    def apply(self, statement, model):
        """Add `client_id = ? AND project_id = ?` for a tenant-owned model."""
        return statement.where(model.client_id == self.client_id, model.project_id == self.project_id)


class BaseRepository(Generic[M]):
    model: type[M]

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, entity_id) -> Optional[M]:
        return await self.session.get(self.model, entity_id)

    async def add(self, entity: M) -> M:
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def delete(self, entity: M) -> None:
        await self.session.delete(entity)


class TenantRepository(BaseRepository[M]):
    """For tables with client_id + project_id columns."""

    def __init__(self, session: AsyncSession, scope: TenantScope):
        super().__init__(session)
        self.scope = scope

    def scoped(self):
        return self.scope.apply(select(self.model), self.model)

    async def get_scoped(self, entity_id) -> Optional[M]:
        """Get by id only if it belongs to this tenant scope (otherwise None, indistinguishable from not found)."""
        entity = await self.session.get(self.model, entity_id)
        if entity is None or entity.client_id != self.scope.client_id or entity.project_id != self.scope.project_id:
            return None
        return entity


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_username(self, username: str) -> Optional[User]:
        return (await self.session.exec(select(User).where(User.username == username))).first()

    async def list_for_client(self, client_id: int) -> Sequence[User]:
        statement = select(User).where(User.client_id == client_id).order_by(col(User.username))
        return (await self.session.exec(statement)).all()

    async def get_in_client(self, user_id: int, client_id: int) -> Optional[User]:
        """Get by id only inside this client (otherwise None, indistinguishable from not found)."""
        user = await self.session.get(User, user_id)
        return user if user is not None and user.client_id == client_id else None

    async def count_active_admins(self, client_id: int, admin_role: str) -> int:
        statement = select(User.id).where(User.client_id == client_id, User.role == admin_role,
                                          col(User.is_active).is_(True))
        return len((await self.session.exec(statement)).all())

    async def mark_login(self, user: User) -> None:
        user.last_login_at = utcnow()
        self.session.add(user)


class ClientRepository(BaseRepository[Client]):
    model = Client


class ProjectRepository(BaseRepository[Project]):
    model = Project

    async def list_for_user(self, user_id: int, client_id: int) -> Sequence[Project]:
        statement = (select(Project).join(ProjectMember, ProjectMember.project_id == Project.id)
                     .where(ProjectMember.user_id == user_id, Project.client_id == client_id,
                            col(Project.is_active).is_(True))
                     .order_by(col(Project.name)))
        return (await self.session.exec(statement)).all()


    async def list_active_for_client(self, client_id: int) -> Sequence[Project]:
        statement = (select(Project).where(Project.client_id == client_id, col(Project.is_active).is_(True))
                     .order_by(col(Project.name)))
        return (await self.session.exec(statement)).all()


class MembershipRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, user_id: int, project_ids: Sequence[int]) -> None:
        for project_id in project_ids:
            self.session.add(ProjectMember(project_id=project_id, user_id=user_id))
        await self.session.flush()

    async def is_member(self, user_id: int, client_id: int, project_id: int) -> bool:
        statement = (select(ProjectMember.project_id)
                     .join(Project, Project.id == ProjectMember.project_id)
                     .join(Client, Client.id == Project.client_id)
                     .where(ProjectMember.user_id == user_id, Project.id == project_id, Project.client_id == client_id,
                            col(Project.is_active).is_(True), col(Client.is_active).is_(True)))
        return (await self.session.exec(statement)).first() is not None
