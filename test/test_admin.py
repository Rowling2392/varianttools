#/usr/bin/env python

# $file: test_admin.py $
# $lastChangedDate: 2012-05-14 $

# This file is part of variant_tools, a software application to annotate,
# summarize, and filter variants for next-gen sequencing ananlysis.
# Please visit http://varianttools.sourceforge.net for details.
#
# Copyright (C) 2011 Bo Peng (bpeng@mdanderson.org)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#

import os
import glob
import unittest
import subprocess
from testUtils import ProcessTestCase, runCmd, numOfVariant, numOfSample, getGenotypes, getSamplenames, output2list, getGenotypeInfo

class TestAdmin(ProcessTestCase):

     def setUp(self):
         'Create a project'
         runCmd('vtools init test -f')

     def removeProj(self):
         runCmd('vtools remove project')

     def TestMerge_SameTable(self):
         'Test command admin'
         self.assertFail('vtools admin')
         self.assertSucc('vtools admin -h')
         self.assertSucc('vtools import vcf/CEU.vcf.gz --build hg18')
         self.assertEqual(numOfSample(), 60)
         self.assertEqual(numOfVariant(),288)
         self.assertSucc('vtools admin --rename_samples \'sample_name like "%NA069%"\' NA06900')
         #could not merge them together if they are from the same table.
         self.assertFail('vtools admin --merge_samples')

     def TestMerge_DiffTable(self):
         'Test command admin'
         runCmd('vtools import vcf/CEU.vcf.gz --build hg18') 
         runCmd('vtools import vcf/SAMP1.vcf  --build hg18')
         self.assertEqual(numOfVariant(),577)
         self.assertEqual(numOfSample(), 61)
         runCmd('vtools admin --rename_samples \'filename like "%SAMP1%"\' NA06985')
         self.assertEqual(numOfSample(), 61)
         runCmd('vtools admin --merge_samples')         
         self.assertEqual(numOfVariant(),577)
         self.assertEqual(numOfSample(), 60)

     def TestMerge_DiffTableFail(self):
         self.assertSucc('vtools import vcf/SAMP2.vcf --build hg18')
         self.assertSucc('vtools import vcf/SAMP1.vcf  --build hg18')
         self.assertSucc('vtools admin --rename_samples \'filename like "%2%"\' SAMP1')
         self.assertFail('vtools admin --merge_samples')
         #the reason is that the two samepls have some identical variants. If you want to merge them, the samples should have different unique variant information.

if __name__ == '__main__':
    unittest.main()