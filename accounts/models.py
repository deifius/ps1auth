from django.conf import settings
from django.core.cache import cache
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.db import models
from ldap3 import BASE, MODIFY_ADD, MODIFY_DELETE, MODIFY_REPLACE, ALL_ATTRIBUTES, LEVEL
from ldap3.utils.conv import escape_bytes
from ldap3.utils.dn import escape_attribute_value
from ldap3.core.exceptions import LDAPBindError, LDAPInvalidCredentialsResult, LDAPEntryAlreadyExistsResult, LDAPNoSuchAttributeResult
import uuid
from .backends import PS1Backend, get_ldap_connection


class PS1UserManager(BaseUserManager):

    def create_user(self, username, email = None, first_name = None, last_name = None, password = None):
        dn = "CN={0},{1}".format(username, settings.AD_BASEDN)
        object_class = ['top', 'person', 'organizationalPerson', 'user']
        attributes = {
            'cn':  username,
            'userPrincipalName': username + '@' + settings.AD_DOMAIN,
            'sAMAccountName': username,
            'userAccountControl': '514',
        }

        # Our forms will always define these, but django gets unhappy if you require
        # more than a username and password
        if first_name:
            attributes['givenName'] = first_name
        if last_name:
            attributes['sn'] = last_name
        if email:
            attributes['mail'] = email

        # prep account enable
        enable_account_changelist = {
            'userAccountControl': (MODIFY_REPLACE, ['512'])
        }

        ldap_connection = get_ldap_connection()

        # add the user to AD
        with ldap_connection as c:
            c.add(dn, object_class, attributes)


        #now get the user guid
        with ldap_connection as c:
            c.search(dn, '(objectClass=*)', BASE, attributes=['objectGUID'])
            response = c.response

        guid_bytes = response[0]['attributes']['objectGUID'][0]
        guid = uuid.UUID(bytes_le=guid_bytes)

        user = PS1Backend().get_user(guid)
        user.save()

        #set password
        if password:
            user.set_password(password)

        #turn the account on
        with ldap_connection as c:
            c.modify(dn, enable_account_changelist)
            response = c.response
            result = c.result
        user._expire_ldap_data()
        return user

    def delete_user(self, user):
        l = get_ldap_connection()
        user_dn = user.ldap_user['distinguishedName'][0]
        result = l.delete(user_dn)
        user.delete()

    def create_superuser(self, object_guid, password = None, email = None, first_name = None, last_name = None):
        """
        object_guid is actually a username. calling it object_guid gets around
        a bug in ./manage.py createsuperuser
        """
        user = self.create_user(object_guid, email=email, password=password)
        admins_dn = "CN={0},{1}".format("Domain Admins", settings.AD_BASEDN)
        user_dn = user.ldap_user['distinguishedName'][0]

        add_to_group_changelist = {
            'member': (MODIFY_ADD, [user_dn])
        }

        with get_ldap_connection() as c:
            c.modify(admins_dn, add_to_group_changelist)

        user._expire_ldap_data()
        return user

    def get_users_by_field(self, field, value):
        escaped_value = escape_attribute_value(value)
        filter_string = "({0}={1})".format(field, escaped_value)
        with get_ldap_connection() as c:
            c.search(settings.AD_BASEDN, filter_string, LEVEL, attributes=['objectGUID'])
            result = c.response
        backend = PS1Backend()
        users = []
        for ldap_user in result:
            guid = uuid.UUID(bytes_le=(ldap_user['attributes']['objectGUID'][0]))
            users.append(backend.get_user(str(guid)))
        return users


class PS1User(AbstractBaseUser):
    """ Represents a User
    """

    objects = PS1UserManager()
    object_guid = models.CharField(
            verbose_name="Username",
            max_length=48,
            primary_key=True,
            unique=True,
            db_index=True,
            editable=False,
        )
    USERNAME_FIELD = 'object_guid'

    def get_full_name(self):
        if not self.ldap_user:
            return repr(self)
        try:
            first_name = self.ldap_user['givenName'][0]
            last_name = self.ldap_user['sn'][0]
        except KeyError:
            return repr(self)
        return ("{0} {1}").format(first_name, last_name)

    def get_short_name(self):
        if self.ldap_user:
            return self.ldap_user['cn'][0]
        else:
            return "AD User Set, but not found"

    def check_password(self, raw_password):
        username = self.ldap_user['userPrincipalName'][0]
        try:
            get_ldap_connection(username, raw_password)
            return True
        except (LDAPBindError, LDAPInvalidCredentialsResult):
            return False

    def set_password(self, raw_password):
        l = get_ldap_connection()
        password_value=  '"{}"'.format(raw_password).encode('utf-16-le')

        password_changes = {
            'unicodePwd':  (MODIFY_REPLACE, [password_value])
        }

        dn = self.ldap_user['distinguishedName'][0]
        with get_ldap_connection() as c:
            c.modify(dn, password_changes)
            response = c.response
            result = c.result

    def set_unusable_password(self):
        raise NotImplementedError

    def has_usable_password(self):
       return self.is_active

    @property
    def is_superuser(self):
        return True

    def has_perm(self, perm, obj=None):
        return True

    def has_perms(self, perm_list, obj=None):
        #HEFTODO fix this
        return True

    def has_module_perms(self, package_name):
        #HEFTODO fix this
        return True

    @property
    def is_active(self):
        return (int(self.ldap_user['userAccountControl'][0]) & 2) != 2

    @property
    def is_staff(self):
        domain_admins_dn = "CN=Domain Admins,{}".format(settings.AD_BASEDN)
        try:
            return domain_admins_dn in self.ldap_user['memberOf']
        except KeyError:
            return False

    @property
    def groups(self):
        try:
            return PS1Group.objects.filter(dn__in=self.ldap_user['memberOf'])
        except KeyError:
            return []

    @property
    def ldap_user(self):
        if hasattr(self, '_ldap_user'):
            return self._ldap_user
        self._ldap_user = cache.get(self.object_guid)
        if not self._ldap_user:
            guid = uuid.UUID(self.object_guid)
            # certain byte sequences contain printable character that can
            # potentially be parseable by the query string.  Escape each byte as
            # hex to make sure this doesn't happen.
            #restrung = ''.join(['\\%02x' % ord(x) for x in guid.bytes_le])
            filter_string = '(objectGUID={})'.format(escape_bytes(guid.bytes_le))
            with get_ldap_connection() as c:
                c.search(settings.AD_BASEDN, filter_string, LEVEL, attributes = ALL_ATTRIBUTES)
                result = c.response

            if len(result) > 0:
                self._ldap_user = result[0]['attributes']
                cache.set(self.object_guid, self._ldap_user, 24 * 60 * 60 * 70)
        return self._ldap_user

    @property
    def email(self):
        if self.ldap_user:
            return self.ldap_user.get('mail', [''])[0]
        else:
            return None

    def _expire_ldap_data(self):
        if hasattr(self, '_ldap_user'):
            del(self._ldap_user)
        cache.delete(self.object_guid)

    def __str__(self):
        return self.get_short_name()

class PS1GroupManager(models.Manager):

    def create_group(self, group_name):
        dn = "CN={0},{1}".format(group_name, settings.AD_BASEDN)
        object_class = ['top', 'group']
        attributes = {
            'cn':  group_name,
            'sAMAccountName': group_name,
            'msSFU30Name': group_name,
        }

        ldap_connection = get_ldap_connection()

        with ldap_connection as c:
            c.add(dn, object_class, attributes)

        ps1group = PS1Group(dn=dn,display_name=group_name)
        ps1group.save()
        return ps1group

    def delete_group(self, group):
        l = get_ldap_connection()
        result = l.delete(group.dn)
        group.delete()

class PS1Group(models.Model):
    objects = PS1GroupManager()
    dn = models.CharField(max_length=255, unique=True)
    display_name = models.CharField(max_length=255)

    class Meta:
        verbose_name = 'PS1 Group'

    def add_user(self, user):
        user_dn = user.ldap_user['distinguishedName'][0]
        add_to_group_changelist = {
            'member': (MODIFY_ADD, [user_dn])
        }
        with get_ldap_connection() as c:
            try:
                c.modify(self.dn, add_to_group_changelist)
            except LDAPEntryAlreadyExistsResult:
                # User is already in the group
                pass
            user._expire_ldap_data()
            return c.result

    def remove_user(self, user):
        user_dn = user.ldap_user['distinguishedName'][0]
        remove_from_group_changelist = {
            'member': (MODIFY_DELETE, [user_dn])
        }
        with get_ldap_connection() as c:
            try:
                c.modify(self.dn, remove_from_group_changelist)
            except LDAPNoSuchAttributeResult:
                # User is not in the group
                pass
            user._expire_ldap_data()
            return c.result

    def has_user(self, user):
        try:
            return self.dn in user.ldap_user['memberOf']
        except KeyError:
            return False

    def __str__(self):
        return self.display_name

def gen_uuid():
    return str(uuid.uuid4())

class Token(models.Model):
    user = models.ForeignKey('PS1User')
    key = models.CharField(max_length=36, default=gen_uuid, editable=False)
    timestamp = models.DateTimeField(auto_now_add=True)
