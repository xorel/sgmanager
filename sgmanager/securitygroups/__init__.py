# -*- coding: utf-8 -*-
# Copyright (C) 2007-2013, GoodData(R) Corporation. All rights reserved

import os
import re
import logging

import yaml

import sgmanager
from sgmanager.decorators import CachedMethod
from sgmanager.securitygroups.sgroup import SGroup
from sgmanager.securitygroups.srule import SRule

from sgmanager.exceptions import InvalidConfiguration


lg = logging.getLogger(__name__)


class SecurityGroups(object):
    def __init__(self, vpc=False, only_groups=[]):
        """
        Create instance, save ec2 connection
        """
        global ec2
        ec2 = sgmanager.ec2

        self.vpc = vpc
        self.only_groups = only_groups

        self.groups = {}
        self.config = None
        try:
            self.owner_id = ec2.get_all_security_groups('default')[0].owner_id
            lg.debug("Default owner id: %s" % self.owner_id)
        except Exception as e:
            lg.error("Can't load default security group to lookup owner id: %s" % e)

    def load_remote_groups(self):
        """
        Load security groups from EC2
        Convert boto.ec2.securitygroup objects into unified structure
        :rtype : list
        """
        lg.debug("Loading remote groups")
        groups = ec2.get_all_security_groups(self.only_groups)
        for group in groups:
            if not self.vpc and group.vpc_id:
                lg.debug("Skipping VPC group %s" % group.name)
                continue
            elif self.vpc and not group.vpc_id:
                lg.debug("Skipping non-VPC group %s" % group.name)
                continue

            # Initialize SGroup object
            sgroup = SGroup(sgroup_object=group)

            # For each egress rule in group
            for rule in group.rules_egress:
                self._load_remote_rule(rule, sgroup, egress=True)

            # For each ingress rule in group
            for rule in group.rules:
                self._load_remote_rule(rule, sgroup)

            self.groups[sgroup.name] = sgroup

        return self.groups

    def _load_remote_rule(self, rule, sgroup, egress=False):
        """
        Create SRule objects from single remote rule and add them to sgroup
        """
        rule_info = {
            'protocol': str(rule.ip_protocol),
            'srule_object': rule,
        }

        if egress:
            rule_info['egress'] = egress

        if sgroup.vpc_id and not rule.to_port:
            # We are VPC rule without port (probably icmp), it's fine..
            pass
        else:
            # Non-VPC rule requires port
            if rule.from_port != rule.to_port:
                # We have port range, use port_from and port_to parameters
                rule_info['port_from'] = int(rule.from_port)
                rule_info['port_to'] = int(rule.to_port)
            else:
                # We have single port, use port parameter
                rule_info['port'] = int(rule.to_port)

        if rule.grants:
            for grant in rule.grants:
                # For each granted permission, add new SRule
                try:
                    srule = SRule(owner_id=self.owner_id, groups={
                        'name'  : str(grant.groupName),
                        'owner' : str(grant.owner_id),
                        # OpenStack doesn't support group IDs, use None if this attr isn't present
                        'id'    : None if getattr(grant, 'groupId', None) is None else str(getattr(grant, 'groupId'))
                    }, **rule_info)
                except AttributeError:
                    srule = SRule(cidr=[ str(grant.cidr_ip) ], **rule_info)

                sgroup.add_rule(srule)

    def load_local_groups(self, config, mode):
        """
        Load local groups from config file
        Save and return SecurityGroups object

        :param config:
        :rtype : object
        """
        lg.debug("Loading local configuragion")
        self.config = config
        yaml.add_constructor('!include', self._yaml_include)

        try:
            with open(config, 'r') as fp:
                conf = yaml.load(fp)
        except IOError as e:
            # Error while loading file
            raise InvalidConfiguration("Can't read config file %s: %s" % (config, e))
        except yaml.YAMLError as e:
            # Error while parsing YAML
            if hasattr(e, 'problem_mark'):
                mark = e.problem_mark
                raise InvalidConfiguration("Can't parse config file %s: error at line %s, column %s" % (config, mark.line+1, mark.column+1))
            else:
                raise InvalidConfiguration("Can't parse config file %s: %s" % (config, e))

        # Remove include keys
        conf = self._fix_include(conf)

        lg.debug("Loading local groups")
        for name, group in conf.iteritems():
            if not self.only_groups or name in self.only_groups:
                # Initialize SGroup object
                self.groups[name] = self._load_sgroup(name, group, check_mode=mode)

        return self.groups

    def _load_sgroup(self, name, group, check_mode='strict'):
        """
        Return SGroup object with assigned rules
        """
        # Test len of name and description
        if len(name) > 255:
            raise InvalidConfiguration("Name of group '%s' is longer than 255 chars" % (name))
        if group.has_key('description') and len(group['description']) > 255:
            raise InvalidConfiguration("Description of group '%s' is longer than 255 chars" % (name))

        # Test name and description according to spec
        if check_mode == 'strict':
            allowed = '^[a-zA-Z0-9_\- ]+$'
        if check_mode == 'ascii':
            allowed = '^[\x20-\x7E]+$'
        if check_mode == 'vpc':
            allowed = '^[a-zA-Z0-9 ._\-:/()#,@[\]+=&;{}!$*]+$'

        if group.has_key('description') and not re.match(allowed, group['description']):
            raise InvalidConfiguration("Description of group '%s' is not valid in %s check_mode" % (name, check_mode))

        if not re.match(allowed, name):
            raise InvalidConfiguration("Name of group '%s' is not valid in %s check_mode" % (name, check_mode))

        sgroup = SGroup(name,
                        description=None if 'description' not in group.keys() else group['description'],
                        vpc_id=None if 'vpc_id' not in group.keys() else group['vpc_id'])

        # Dive into group's rules and create srule objects
        if group.has_key('rules'):
            for rule in group['rules']:
                self._load_rule(sgroup, rule)

        return sgroup

    def _load_rule(self, sgroup, rule):

        def _expand_to_and_load(sgroup, r):
                if r.has_key('to'):
                    # expand also "to:" section
                    for proto_port in r['to']:
                        new_r = r.copy()
                        new_r.pop('to')
                        new_r.update(proto_port)
                        srule = SRule(owner_id=self.owner_id, **new_r)
                        sgroup.add_rule(srule)
                else:
                    srule = SRule(owner_id=self.owner_id, **r)
                    sgroup.add_rule(srule)

        for expand_key in ['cidr', 'groups']:
            for cidr_or_group in rule.get(expand_key, []):
                #create new rule
                new_rule = { expand_key: [cidr_or_group] }

                # copy the rest
                for key in ['port', 'protocol', 'port_from', 'port_to', 'to']:
                    if key in rule: 
                        new_rule[key] = rule[key]

                _expand_to_and_load(sgroup, new_rule)

            if not rule.has_key('cidr') and not rule.has_key('groups'):
                _expand_to_and_load(sgroup, rule)



    def _yaml_include(self, loader, node):
        """
        Include another yaml file from main file
        This is usually done by registering !include tag
        """
        filepath = "%s%s" % ('%s/' % os.path.dirname(self.config) \
                                if os.path.dirname(self.config) else '',
                             node.value)
        try:
            with open(filepath, 'r') as inputfile:
                return yaml.load(inputfile)
        except IOError as e:
            raise InvalidConfiguration("Can't include config file %s: %s" % (filepath, e))

    def _fix_include(self, cfg):
        """ Special hack to use included parameters correctly """
        for key, value in cfg.items():
            if key == "include":
                # Fix include
                for include in value:
                    if isinstance(include, dict):
                        # Go deeper
                        include = self._fix_include(include)
                    cfg = self._dict_update(cfg, include)
                cfg.pop('include')
                continue

            if isinstance(value, dict):
                # Go deeper
                cfg[key] = self._fix_include(value)
        return cfg

    def _dict_update(self, dict1, dict2, overwrite=False, skip_none=False):
        """
        Update dictionary recursively no matter on it's content
        dict1 or main dictionary that we want to update by dict2
        if overwrite is True, we will overwrite values in dict1,
        otherwise we won't touch them
        """
        # Clone dictionaries to abandon references
        dict1 = dict(dict1)
        dict2 = dict(dict2)

        for key, value in dict2.items():
            if value is None and skip_none:
                continue
            if isinstance(value, dict) and dict1.has_key(key):
                # We have another dictionary to update recursively
                dict1[key] = dict_update(dict1[key], value, overwrite)
            else:
                if dict1.has_key(key) and not overwrite:
                    # We don't want to overwrite values in dict1
                    continue
                else:
                    dict1[key] = value

        return dict1

    def dump_groups(self):
        """
        Return YAML dump of loaded groups
        :rtype : basestring
        """
        return yaml.dump( dict((name, group.dump()) for (name, group) in self.groups.iteritems() ), Dumper=YamlDumper)
        # from pprint import pprint as pp
        # return dict((name, group.dump()) for (name, group) in self.groups.iteritems() )

    def has_group(self, name):
        if self.groups.has_key(name):
            return True
        else:
            return False

    def __eq__(self, other):
        """
        Equal matching, just call self.compare and return boolean
        """
        added, removed, updated, unchanged = self.compare(other)
        if added or removed or updated:
            return False
        else:
            return True

    def __ne__(self, other):
        """
        Not equal matching, call __eq__ but reverse output
        """
        return not self.__eq__(other)

    @CachedMethod
    def compare(self, other):
        """
        Compare SecurityGroups objects

        Return tuple of lists with SGroups objects (added, removed, updated, unchanged)
        """
        if not isinstance(other, SecurityGroups):
            raise TypeError("Compared object must be instance of SecurityGroups, not %s" % type(other).__name__)

        added = []
        removed = []
        updated = []
        unchanged = []

        for name, group in self.groups.iteritems():
            # Group doesn't exist in target SecurityGroups object
            if not other.has_group(name):
                added.append(group)
                continue

            # Compare matched group
            if group != other.groups[name]:
                # Differs - update
                updated.append(group)
            else:
                # Group is same as other one - unchanged
                unchanged.append(group)

        # Check if some groups shouldn't be removed
        for name, group in other.groups.iteritems():
            if not self.has_group(name):
                removed.append(group)

        return added, removed, updated, unchanged


class YamlDumper(yaml.SafeDumper):
    """
    Custom YAML dumper that will ignore aliases
    """
    def ignore_aliases(self, _data):
        return True
