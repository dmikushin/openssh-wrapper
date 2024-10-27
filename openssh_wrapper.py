# -*- coding: utf-8 -*-
"""
This is a wrapper around the openssh binaries ssh and scp.
"""
import io
import re
import os
import os.path
import sys
import pipes
import signal
import shutil
import getpass
import tempfile
import subprocess

__all__ = 'SSHConnection SSHForwardingTunnel SSHRevForwardingTunnel SSHResult SSHError b u b_list u_list'.split()

if sys.version[0] == '2':
    text = unicode
    bytes = str
else:  # PY3K
    text = str


def b(string):
    """
    convert string (unicode, str or bytes) to binary representation
    """
    if isinstance(string, bytes):
        return string
    return string.encode('utf-8')


def u(string):
    """
    convert string (unicode, str or bytes) to textual representation
    """
    if isinstance(string, text):
        return string
    return string.decode('utf-8')


def b_list(items):
    """
    convert all items of the list to binary representation
    """
    return [b(item) for item in items]


def u_list(items):
    """
    convert all items of the list to textual representation
    """
    return [u(item) for item in items]


def b_quote(cmd_chunks):
    """
    Given a list of commands (unicode or text strings), return the safe list,
    suitable to be passed to subprocess
    """
    quoted = []
    for chunk in cmd_chunks:
        # pipes.quote works with text representation only
        quoted.append(b(pipes.quote(u(chunk))))
    return b(' ').join(quoted)

class _SSHTunnel(object):

    def __init__(self, local_addr='localhost', local_port=0, remote_addr='localhost', remote_port=0):
        """
        Set up an SSHTunnel object.

        :param local_addr: bind forwarding tunnel to this local address
        :param local_port: bind forwarding tunnel to this local port
        :param remote_addr: direct connections through the forwarding tunnel to this remote address
        :param remote_port: direct connections through the forwarding tunnel to this remote port
        """
        if local_port == 0 or remote_port == 0:
            raise SSHError('SSHTunnel objects require the local port and the remote port to be set and non-zero.')

        self.local_addr = local_addr
        self.local_port = local_port
        self.remote_addr= remote_addr
        self.remote_port = remote_port

    def __str__(self):
        """
        Generate the tunnel string portion required in the SSH command line.
        """
        return ('{l_a}:{l_p}:{r_a}:{r_p}'.format(l_a=self.local_addr, l_p=self.local_port, r_a=self.remote_addr, r_p=self.remote_port))

class SSHForwardingTunnel(_SSHTunnel):

    def __init__(self, *args, **kwargs):
        """
        Set up a port forwarding (openssh -L) tunnel.

        :param local_addr: bind forwarding tunnel to this local address
        :param local_port: bind forwarding tunnel to this local port
        :param remote_addr: direct connections through the forwarding tunnel to this remote address
        :param remote_port: direct connections through the forwarding tunnel to this remote port
        """
        _SSHTunnel.__init__(self, *args, **kwargs)

    def __str__(self):
        """
        Generate a forwarding tunnel option command line string for the SSH command.
        """
        return '-L {tunnel}'.format(tunnel=_SSHTunnel.__str__(self))

class SSHRevForwardingTunnel(_SSHTunnel):

    def __init__(self, *args, **kwargs):
        """
        Set up a reverse port forwarding (openssh -R) tunnel.

        :param local_addr: direct connections through the reverse forwarding tunnel to this local address
        :param local_port: direct connections through the reverse forwarding tunnel to this local port
        :param remote_addr: bind reverse forwarding tunnel to this address on the remote system
        :param remote_port: bind reverse forwarding tunnel to this port on the remote system
        """
        _SSHTunnel.__init__(self, *args, **kwargs)

    def __str__(self):
        """
        Generate a reverse forwarding tunnel option command line string for the SSH command.
        """
        return '-R {tunnel}'.format(tunnel=_SSHTunnel.__str__(self))


class SSHConnection(object):
    """
    This class holds all values needed to connect to a host via ssh.
    It provides methods for command execution and file transfer via scp.
    """

    def __init__(self, server, login=None, port=None, configfile=None,
                 identity_file=None, ssh_agent_socket=None, timeout=60, debug=False,
                 options=[],
                 master=False, slave=False, control_path=None):
        """
        Create new object to establish SSH connection to remote servers

        :param server: server name or IP address to send commands to (required)
        :param login: user login (by default current login is used)
        :param port: SSH port number. Optional.
        :param configfile: local configuration file (by default ~/.ssh/config is used)
        :param identity_file:  address of the socket to connect to ssh agent,
        :param options: pass-through SSH client config options directly to openSSH
            (caution: no sanity checks is being done for these options)
        :param master: run SSH in master mode (i.e. enable connection sharing, other
            slave SSHConnection instances can use this SSHConnection as their master
            SSHConnection)
        :param slave: run SSH in slave mode (i.e. share the connection with an already
            established SSHConnection running in master mode)
        :param control_path: a path containing an existing directory and a socket file
            name to be used as the master/slave connection's control path

        if you want to use it. ``SSH_AUTH_SOCK`` environment variable is
        used if None is supplied.
        :param ssh_agent_socket: address of the socket to connect to ssh agent
        :param timeout: connect timeout. If you plan to execute long
        lasting commands, adjust this variable accordingly.  Default value of
        60 seconds is usually a good choice.

        :raise SSHError: if server name or login contain illegal symbols, or
        some of the files, provided to the constructor, don't exist.

        .. note:: `man ssh_config` is highly recommended amendment to this
                   command.
        """
        self.server = server
        self.port = port
        self.timeout = timeout
        self.check_server(server)
        self.user = getpass.getuser()
        self.debug = debug

        # master / slave connections
        self.master = master
        self.control_path = control_path
        self.slave = slave

        self.check_master_slave_settings()

        if configfile:
            self.configfile = os.path.expanduser(configfile)
            if not os.path.isfile(self.configfile):
                raise SSHError('Config file %s is not found' % self.configfile )
        else:
            self.configfile = None

        self.login = None
        self.identity_file = None
        self.ssh_agent_socket = None
        self.options = options

        self.tunneling_pipes = []

        if not slave:

            # this is only needed for master sessions or for session not in master/slave mode
            # slave-only SSHConnection instance don't need the below auth data...
            if login:
                self.check_login(login)
                self.login = b(login)
            if identity_file:
                self.identity_file = os.path.expanduser(identity_file)
                if not os.path.isfile(self.identity_file):
                    raise SSHError('Key file %s is not found' % self.identity_file)
            self.ssh_agent_socket = ssh_agent_socket

        if self.master:
            init_master_ssh_command = self.ssh_command(init_master=True)

            self.master_ssh_pipe = subprocess.Popen(init_master_ssh_command,
                                                stdin=None,
                                                stdout=None,
                                                stderr=None,
                                                env=self.get_env())

    def __del__(self):
        """
        SSHConnection destructor method
        """
        # take down all SSH forwarding tunnels
        for tunneling_pipe in self.tunneling_pipes:
            # empty the stdout, stderr bufffer of the subprocess PIPE
            tunneling_pipe.terminate()
            tunneling_pipe.communicate()

        if self.master:
            # take down SSH master process...
            if hasattr(self, 'master_ssh_pipe') and self.master_ssh_pipe:
                self.master_ssh_pipe.terminate()
                self.master_ssh_pipe.communicate()

    def check_server(self, server):
        """
        Check the server string for illegal characters.

        :param server: a string with server name
        :return: None
        :raise: SSHError
        """
        if not re.compile(r'^[a-zA-Z0-9.\-_]+$').match(server):
            raise SSHError('Server name contains illegal symbols')

    def check_login(self, login):
        """
        Check the login string for illegal characters.

        :param login: a string with user login
        :return: None
        :raise: SSHError
        """
        if not re.compile(r'^[a-zA-Z0-9.\-_]+$').match(login):
            raise SSHError('User login contains illegal symbols')

    def check_master_slave_settings(self):
        """
        Run some sanity checks on the provided master / slave connection related
        parameters.
        """

        if self.master or self.slave:
            if self.control_path is None:
                raise SSHError('SSHConnection needs a control path when run in master or slave mode.')

            if not os.path.isdir(os.path.dirname(self.control_path)):
                raise SSHError('SSHConnection expects that a directory for the control_path file already exists.')

        if self.control_path:
            if type(self.control_path) == str:
                self.control_path = os.path.expanduser(self.control_path)
            else:
                raise SSHError('Config file %s is not found' % self.configfile )

    def run(self, command, interpreter='/bin/bash', forward_ssh_agent=False, tunnels=[]):
        """
        Execute the command using the interpreter provided

        Consider this roughly as::

            echo "command" | ssh root@server "/bin/interpreter"

        Hint: Try interpreter='/usr/bin/python'

        :param command: string/unicode object or byte sequence with the command
        or set of commands to execute
        :param interpreter: name of the interpreter (by default "/bin/bash" is used)
        :param forward_ssh_agent: turn this flag to `True`, if you want to use
        and forward SSH agent
        :param tunnels: single tunnel or a list of tunnels. The tunnels
            have to be :class:`_SSHTunnel` derived objects. NOTE: using tunnels
            in the :func:`run()` method only makes sense for commands that launch
            a long running remote process. Possibly, you may rather be looking for
            the :func:`run_tunnels()' method.
        :return: SSH result instance
        :rtype: SSHResult

        :raise: SSHError, if server is unreachable, or timeout has reached.
        """

        # master-only SSHConnection instances cannot be used to run commands
        if self.master and not self.slave:
            raise SSHError('This SSHConnection is an SSH master connection, no commands can be sent to the server on this SSHConnection.')

        ssh_command = self.ssh_command(interpreter=interpreter, forward_ssh_agent=forward_ssh_agent, tunnels=[])
        pipe = subprocess.Popen(ssh_command,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=self.get_env())
        try:
            signal.signal(signal.SIGALRM, _timeout_handler)
        except ValueError:  # signal only works in main thread
            pass
        signal.alarm(self.timeout)
        out = b('')
        err = b('')
        try:
            out, err = pipe.communicate(b(command))
        except IOError as exc:
            # pipe.terminate() # only in python 2.6 allowed
            os.kill(pipe.pid, signal.SIGTERM)
            signal.alarm(0)  # disable alarm
            raise SSHError("%s (under %s): %s" % (
                ' '.join(u_list(ssh_command)), self.user, str(exc)))

        signal.alarm(0)  # disable alarm
        returncode = pipe.returncode
        if returncode == 255:  # ssh client error
            raise SSHError("%s (under %s): %s" % (
                ' '.join(u_list(ssh_command)), self.user, err.strip()))
        return SSHResult(command, out.strip(), err.strip(), returncode)


    def run_tunnels(self, tunnels):
        """

        Run one or more port forwarding / reverse forwarding tunnels in a
        separate process without interaction with a remote shell.

        :param tunnels: single tunnel or a list of tunnels. The tunnels
            have to be :class:`_SSHTunnel` derived objects

        :return: the :class:`subprocess.Popen` pipe object that was used to
            launch the tunnel(s) (and that can be used to take it/them down
            again)
        :rtype: ``obj``
        """
        if type(tunnels) not in (list, tuple):
            tunnels = [tunnels]

        ssh_command = self.ssh_command(tunnels=tunnels)

        tunneling_pipe = subprocess.Popen(ssh_command,
                                          stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE,
                                          env=self.get_env())

        # collect the tunneling pipes...
        self.tunneling_pipes.append(tunneling_pipe)

        return tunneling_pipe

    def scp(self, files, target, mode=None, owner=None):
        """ Copy files identified by their names to remote location

        .. note:: if you want your file objects to have meaningful names,
                  make sure they have `name` attribute.

        :param files: list of file names or file-like objects to copy. Before
        copying the files will be interpreted the following way: if the element
        is a string, it is considered as a file name, if it's a file-like object,
        then it will be copied to a temporary directory, and then copied from
        there to a remote location using "scp" command.

        :param target: target file or directory to copy data to. Target file
        makes sense only if the number of files to copy equals to one.

        :param mode: optional parameter to define mode for every uploaded file
        (must be a string in the form understandable by chmod, like "0644")

        :param owner: optional parameter to define user and group for every
        uploaded file (must be a string in the form understandable by chown).
        Makes sense only if you open your connection as root.

        :return: None
        :raise: SSHError
        """
        # master-only SSHConnection instances cannot be used to do secure copying
        if self.master and not self.slave:
            raise SSHError('This SSHConnection is an SSH master connection, no secure copying can be done over this SSHConnection.')

        if not isinstance(files, (list, tuple)):
            raise SSHError('The files argument to scp() function must be a list or tuple')

        filenames, tmpdir = self.convert_files_to_filenames(files)

        def cleanup_tmp_dir():
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

        scp_command = self.scp_command(filenames, target)
        pipe = subprocess.Popen(scp_command,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=self.get_env())
        try:
            signal.signal(signal.SIGALRM, _timeout_handler)
        except ValueError:  # signal only works in main thread
            pass
        signal.alarm(self.timeout)
        err = b('')
        try:
            _, err = pipe.communicate()
        except IOError as exc:
            # pipe.terminate() # only in python 2.6 allowed
            os.kill(pipe.pid, signal.SIGTERM)
            signal.alarm(0)  # disable alarm
            cleanup_tmp_dir()
            raise SSHError("%s (under %s): %s" % (
                ' '.join(u_list(scp_command)), self.user, str(exc)))
        signal.alarm(0)  # disable alarm
        returncode = pipe.returncode
        if returncode != 0:  # ssh client error
            cleanup_tmp_dir()
            raise SSHError("%s (under %s): %s" % (
                ' '.join(u_list(scp_command)), self.user, err.strip()))

        if mode or owner:
            targets = self.get_scp_targets(filenames, target)
            if mode:
                cmd_chunks = ['chmod', mode] + targets
                cmd = b_quote(cmd_chunks)
                result = self.run(cmd)
                if result.returncode:
                    cleanup_tmp_dir()
                    raise SSHError("change mode: %s" % result.stderr.strip())
            if owner:
                cmd_chunks = ['chown', owner] + targets
                cmd = b_quote(cmd_chunks)
                result = self.run(cmd)
                if result.returncode:
                    cleanup_tmp_dir()
                    raise SSHError("change owner: %s" % result.stderr.strip())
        cleanup_tmp_dir()

    def scp_down(self, remotefile, localtarget, mode=None, owner=None):
        """ Copy files identified by their remote names to local location

        :param remotefile: file name representing the remote file to download
        via scp

        :param target: local file or directory to copy remote file to.

        :param mode: optional parameter to define mode for the downloaded file
        (must be a string in the form understandable by chmod, like "0644")

        :param owner: optional parameter to define user and group for
        downloaded file (must be a string in the form understandable by chown).
        Makes sense only if you open your connection as root.

        :return: None
        :raise: SSHError
        """

        scp_command = self.scp_down_command(remotefile, localtarget)
        if scp_command:
            pipe = subprocess.Popen(scp_command,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, env=self.get_env())
            try:
                signal.signal(signal.SIGALRM, _timeout_handler)
            except ValueError:  # signal only works in main thread
                pass
            signal.alarm(self.timeout)
            err = b('')
            try:
                _, err = pipe.communicate()
            except IOError as exc:
                # pipe.terminate() # only in python 2.6 allowed
                os.kill(pipe.pid, signal.SIGTERM)
                signal.alarm(0)  # disable alarm
                cleanup_tmp_dir()
                raise SSHError("%s (under %s): %s" % (
                    ' '.join(scp_command), self.user, str(exc)))
            signal.alarm(0)  # disable alarm
            returncode = pipe.returncode
            if returncode != 0:  # ssh client error
                cleanup_tmp_dir()
                raise SSHError("%s (under %s): %s" % (
                    ' '.join(scp_command), self.user, err.strip()))

            if mode or owner:
                targets = self.get_scp_targets(filenames, target)
                if mode:
                    cmd_chunks = ['chmod', mode] + targets
                    cmd = b_quote(cmd_chunks)
                    result = self.run(cmd)
                    if result.returncode:
                        cleanup_tmp_dir()
                        raise SSHError("change mode: %s" % result.stderr.strip())
                if owner:
                    cmd_chunks = ['chown', owner] + targets
                    cmd = b_quote(cmd_chunks)
                    result = self.run(cmd)
                    if result.returncode:
                        cleanup_tmp_dir()
                        raise SSHError("change owner: %s" % result.stderr.strip())

    def convert_files_to_filenames(self, files):
        """
        Helper function which is invoked by scp.

        You don't usually need to execute this function manually.
        Check for every file in list and save it locally to send to
        remote side, if needed.

        :param files: list of strings or file-alike objects to be converted
        to filenames

        :return: tuple (filenames, tmpdir), where filenames is a list of absolute
        filenames, and tmpdir is a name of temp directory which must be removed
        afterwards. If tmpdir is None, nothing should be removed.
        """
        filenames = []
        tmpdir = None
        for file_obj in files:
            if isinstance(file_obj, (text, bytes)):
                filenames.append(file_obj)
            else:
                if not tmpdir:
                    tmpdir = tempfile.mkdtemp()
                if hasattr(file_obj, 'name'):
                    basename = os.path.basename(file_obj.name)
                    tmpname = os.path.join(tmpdir, basename)
                    fd = io.open(tmpname, 'wb')
                    fd.write(b(file_obj.read()))
                    fd.close()
                else:
                    tmpfd, tmpname = tempfile.mkstemp(dir=tmpdir)
                    os.write(tmpfd, b(file_obj.read()))
                    os.close(tmpfd)
                filenames.append(tmpname)
        return filenames, tmpdir

    def get_scp_targets(self, filenames, target):
        """
        Given a list of filenames and a target name return the full list of targets

        :param filenames: list of filenames to copy (basenames)
        :param target: target file or directory

        Internal command which is used to perform chmod and chown.

        Example::

            >>> ssh_connection.get_scp_targets(['foo.txt', 'bar.txt'], '/etc')
            ['/etc/foo.txt', '/etc/bar.txt']

            >>> get_scp_targets(['foo.txt', ], '/etc/passwd')
            ['/etc/passwd']
        """
        result = self.run(b('test -d %s' % pipes.quote(u(target))))
        is_directory = result.returncode == 0
        if is_directory:
            ret = []
            for filename in filenames:
                ret.append(os.path.join(target, os.path.basename(filename)))
            return ret
        else:
            return [target, ]

    def ssh_command(self, interpreter=None,
                          forward_ssh_agent=False,
                          init_master=False,
                          tunnels=[],
                   ):
        """
        Build the command string to connect to the server and start the interpreter.

        Internal function
        """
        if not interpreter and \
           not init_master and \
           not tunnels         \
           :
            raise SSHError('SSHConnection.ssh_command(): No interpreter given.')

        cmd = ['/usr/bin/ssh', ]
        if self.debug:
            cmd += ['-vvvv']
        if self.login:
            cmd += ['-l', self.login]
        if self.configfile:
            cmd += ['-F', self.configfile]
        if self.identity_file:
            cmd += ['-i', self.identity_file]
        if forward_ssh_agent:
            cmd.append('-A')
        if self.port:
            cmd += ['-p', str(self.port)]
        if interpreter == None:
            cmd += ['-N']
        if self.master and init_master and self.control_path is not None:
            cmd += ['-M', '-S', self.control_path]
        if self.slave and self.control_path is not None and not init_master:
            cmd += ['-S', self.control_path]
        for tunnel in tunnels:
            cmd += [ str(tunnel) ]
        for option in self.options:
            cmd += ['-o', option]

        cmd.append(self.server)

        if interpreter:
            interpreter = b(interpreter)
            cmd.append(interpreter)

        return b_list(cmd)

    def scp_down_command(self, remotefile, localtarget):
        """
        Build the command string to transfer the file identified by remotefile.

        Include target(s) if specified. Internal function
        """
        if remotefile and localtarget:
            cmd = ['/usr/bin/scp', self.debug and '-vvvv' or '-q', '-r']
            if self.login:
                remotename = '%s@%s' % (u(self.login), u(self.server))
            else:
                remotename = self.server
            if self.configfile:
                cmd += ['-F', self.configfile]
            if self.identity_file:
                cmd += ['-i', self.identity_file]
            if self.port:
                cmd += ['-P', self.port]
            cmd.append('%s:%s' % (remotename, remotefile))
            cmd.append(localtarget)

            return b_list(cmd)
        return None

    def scp_command(self, files, target):
        """
        Build the command string to transfer the files identified by filenames.

        Include target(s) if specified. Internal function
        """
        cmd = ['/usr/bin/scp', self.debug and '-vvvv' or '-q', '-r']
        files = b_list(files)
        if self.login:
            remotename = '%s@%s' % (u(self.login), u(self.server))
        else:
            remotename = self.server
        if self.configfile:
            cmd += ['-F', self.configfile]
        if self.identity_file:
            cmd += ['-i', self.identity_file]
        if self.port:
            cmd += ['-P', str(self.port)]
        for option in self.options:
            cmd += ['-o', option]

        if isinstance(files, (text, bytes)):
            raise ValueError('"files" argument have to be iterable (list or tuple)')
        if len(files) < 1:
            raise ValueError('You should name at least one file to copy')

        cmd += files
        cmd.append('%s:%s' % (remotename, target))
        return b_list(cmd)

    def get_env(self):
        """
        Retrieve environment variables and replace SSH_AUTH_SOCK
        if ssh_agent_socket was specified on object creation.
        """
        env = os.environ.copy()
        if self.ssh_agent_socket:
            env['SSH_AUTH_SOCK'] = self.ssh_agent_socket
        return env


def _timeout_handler(signum, frame):
    """ This function is called when ssh takes too long to connect. """
    raise IOError('SSH connect timeout')


class SSHResult(object):
    """
    Command execution status.
    """
    #: command which has been executed remotely
    command = None
    #: command execution stdout (no charset applied, binary object)
    stdout = None
    #: command execution stderr (no charset applied, binary object)
    stderr = None
    #: command return code (integer, 0 means "success" usually)
    returncode = None


    def __init__(self, command, stdout, stderr, returncode):
        """ Create a new object to hold output and a return code
        to the given command. """
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def __str__(self):
        """
        Get string representation of the result.

        Effectively, returns stdout
        """
        if sys.version[0] == '2':
            # get ASCII representation.
            return self.stdout
        else:
            # get string representation
            return self.stdout.decode('utf-8', 'ignore')

    def __repr__(self):
        """
        Get the verbose interpretation of the object

        For python2.x it's the raw string objects, whereas python3.x
        contains the unicode representation (str)
        """
        if sys.version[0] == '2':
            # get ASCII representation.
            return self.repr_binary()
        else:
            # get string representation
            return self.repr_text()

    def repr_binary(self):
        """ Build simple unicode representation from all member values. """
        ret = []

        ret += [b('command: '), b(self.command), b('\n')]
        ret += [b('stdout: '), b(self.stdout), b('\n')]
        ret += [b('stderr: '), b(self.stderr), b('\n')]
        ret += [b('returncode: '), b(text(self.returncode))]
        return b('').join(ret)

    def repr_text(self):
        return self.repr_binary().decode('utf-8', 'ignore')


class SSHError(Exception):
    """
    This exception is used for all errors raised by this module.
    """
    pass
