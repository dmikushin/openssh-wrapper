# -*- coding: utf-8 -*-
import io
import os
import pytest
import tempfile
from openssh_wrapper import *

test_file = os.path.join(os.path.dirname(__file__), 'tests.py')


def eq_(arg1, arg2):
    assert arg1 == arg2

import getpass
current_user=getpass.getuser()

class TestSSHCommandNames(object):

    def setup_method(self, meth):
        self.c = SSHConnection('localhost', login=current_user,
                               configfile='ssh_config.test')

    def test_ssh_command(self):
        eq_(self.c.ssh_command('/bin/bash', False),
            b_list(['/usr/bin/ssh', '-l', current_user, '-F', 'ssh_config.test', 'localhost', '/bin/bash']))

    def test_scp_command(self):
        eq_(self.c.scp_command(('/tmp/1.txt', ), target='/tmp/2.txt'),
            b_list(['/usr/bin/scp', '-q', '-r', '-F', 'ssh_config.test', '/tmp/1.txt', '{user}@localhost:/tmp/2.txt'.format(user=current_user)]))

    def test_scp_multiple_files(self):
        eq_(self.c.scp_command(('/tmp/1.txt', '2.txt'), target='/home/username/'),
            b_list(['/usr/bin/scp', '-q', '-r', '-F', 'ssh_config.test', '/tmp/1.txt', '2.txt',
                    '{user}@localhost:/home/username/'.format(user=current_user)]))

    def test_scp_targets(self):
        targets = self.c.get_scp_targets(['foo.txt', 'bar.txt'], '/etc')
        eq_(targets, ['/etc/foo.txt', '/etc/bar.txt'])
        targets = self.c.get_scp_targets(['foo.txt'], '/etc/passwd')
        eq_(targets, ['/etc/passwd'])

    def test_simple_command(self):
        result = self.c.run('whoami')
        eq_(result.stdout, b(current_user))
        eq_(result.stderr, b(''))
        eq_(result.returncode, 0)

    def test_python_command(self):
        result = self.c.run('print "Hello world"', interpreter='/usr/bin/python')
        eq_(result.stdout, b('Hello world'))
        eq_(result.stderr, b(''))
        eq_(result.returncode, 0)


def test_timeout():
    c = SSHConnection('example.com', login=current_user, timeout=1)
    with pytest.raises(SSHError):  # ssh connect timeout
        c.run('whoami')


def test_permission_denied():
    c = SSHConnection('localhost', login='www-data', configfile='ssh_config.test')
    with pytest.raises(SSHError):  # Permission denied (publickey)
        c.run('whoami')


class TestSCP(object):

    def setup_method(self, meth):
        self.c = SSHConnection('localhost', login=current_user)
        self.c.run('rm -f /tmp/*.py /tmp/test*.txt')

    def test_scp(self):
        self.c.scp((test_file, ), target='/tmp')
        assert os.path.isfile('/tmp/tests.py')

    def test_scp_int_port(self):
        c = SSHConnection('localhost', login=current_user, port=22)
        c.scp((test_file, ), target='/tmp')
        assert os.path.isfile('/tmp/tests.py')

    def test_scp_str_port(self):
        c = SSHConnection('localhost', login=current_user, port='22')
        c.scp((test_file, ), target='/tmp')
        assert os.path.isfile('/tmp/tests.py')

    def test_scp_to_nonexistent_dir(self):
        with pytest.raises(SSHError):
            self.c.scp((test_file, ), target='/abc/def/')

    def test_mode(self):
        self.c.scp((test_file, ), target='/tmp', mode='0666')
        mode = os.stat('/tmp/tests.py').st_mode & 0o777
        eq_(mode, 0o666)

    def test_owner(self):
        import pwd, grp
        uid, gid = os.getuid(), os.getgid()
        user, group = pwd.getpwuid(uid).pw_name, grp.getgrgid(gid).gr_name
        self.c.scp((test_file, ), target='/tmp', owner='%s:%s' % (user, group))
        stat = os.stat('/tmp/tests.py')
        eq_(stat.st_uid, uid)
        eq_(stat.st_gid, gid)

    def test_file_descriptors(self):
        # name is set explicitly as target
        fd1 = io.BytesIO(b('test'))
        self.c.scp((fd1, ), target='/tmp/test1.txt', mode='0644')
        assert io.open('/tmp/test1.txt', 'rt').read() == 'test'

        # name is set explicitly in the name option
        fd2 = io.BytesIO(b('test'))
        fd2.name = 'test2.txt'
        self.c.scp((fd2, ), target='/tmp', mode='0644')
        assert io.open('/tmp/test2.txt', 'rt').read() == 'test'


class TestSSHMasterSlaveConnections(object):

    def setup_method(self, meth):
        control_path_dir = tempfile.mkdtemp()
        self.control_path='{tmpdir}/control_path.socket'.format(tmpdir=control_path_dir)
        self.c_ms = SSHConnection('localhost', login=current_user,
                               master=True, slave=True, control_path=self.control_path,
                               configfile='ssh_config.test')
        self.c_m = SSHConnection('localhost', login=current_user,
                               master=True, control_path=self.control_path,
                               configfile='ssh_config.test')
        self.c_s = SSHConnection('localhost', login=current_user,
                               slave=True, control_path=self.control_path,
                               configfile='ssh_config.test')

    # MASTER+SLAVE MODE
    # one SSHConnection instance acts as master and slave connection

    def test_masterslave_initmaster_ssh_command(self):
        eq_(self.c_m.ssh_command(init_master=True),
            b_list(['/usr/bin/ssh', '-l', current_user, '-F', 'ssh_config.test', '-N', '-M', '-S', self.control_path, 'localhost']))

    def test_masterslave_ssh_command(self):
        eq_(self.c_ms.ssh_command('/bin/bash', False),
            b_list(['/usr/bin/ssh', '-F', 'ssh_config.test', '-S', self.control_path, 'localhost', '/bin/bash']))

    def test_masterslave_simple_command(self):
        result = self.c_ms.run('whoami')
        eq_(result.stdout, b(current_user))
        eq_(result.stderr, b(''))
        eq_(result.returncode, 0)

    # MASTER-ONLY MODE
    # an SSHConnection instance is in master-only mode

    def test_master_initmaster_ssh_command(self):
        eq_(self.c_m.ssh_command(init_master=True),
            b_list(['/usr/bin/ssh', '-l', current_user, '-F', 'ssh_config.test', '-N', '-M', '-S', self.control_path, 'localhost']))

    # SLAVE-ONLY MODE
    # another SSHConnection instance shared connections with another SSHConnection that runs in master+slave or master-only mode

    def test_slave_ssh_command(self):
        eq_(self.c_s.ssh_command('/bin/bash', False),
            b_list(['/usr/bin/ssh',  '-F', 'ssh_config.test', '-S', self.control_path, 'localhost', '/bin/bash']))

    def test_slave_simple_command(self):
        result = self.c_s.run('whoami')
        eq_(result.stdout, b(current_user))
        eq_(result.stderr, b(''))
        eq_(result.returncode, 0)
