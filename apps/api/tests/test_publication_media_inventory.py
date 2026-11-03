"""Generated local files only; no database, real collection or transport."""
import hashlib
import os
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import patch

from app.services.publication_media_inventory import build_media_inventory, create_verified_media_bundle, MediaInventoryError


class PublicationMediaInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='loci-media-inventory-', dir=(
            os.environ.get('LOCI_TEST_FILE_ROOT') or ('/private/tmp' if os.path.isdir('/private/tmp') else None)))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root/'models').mkdir()
        self.file = self.root/'models/object.glb'
        self.file.write_bytes(b'generated synthetic model bytes')

    def inventory(self, paths=('models/object.glb',)):
        return build_media_inventory(self.root, paths)

    def test_exact_hash_size_and_mtime(self):
        s=self.file.stat();item=self.inventory()['models/object.glb']
        self.assertEqual(item,{'size_bytes':s.st_size,'mtime_ns':s.st_mtime_ns,'sha256':hashlib.sha256(self.file.read_bytes()).hexdigest()})

    def test_changed_bytes_with_identical_size_and_restored_mtime(self):
        before=self.inventory();s=self.file.stat();self.file.write_bytes(b'x'*s.st_size);os.utime(self.file,ns=(s.st_atime_ns,s.st_mtime_ns))
        after=self.inventory();self.assertEqual(before['models/object.glb']['size_bytes'],after['models/object.glb']['size_bytes']);self.assertEqual(before['models/object.glb']['mtime_ns'],after['models/object.glb']['mtime_ns']);self.assertNotEqual(before['models/object.glb']['sha256'],after['models/object.glb']['sha256'])

    def test_empty_record_only_export_needs_no_root(self):
        self.assertEqual(build_media_inventory(self.root/'absent',[]),{})

    def test_invalid_paths_rejected(self):
        for value in ('/absolute','../private','models/../private','models//object.glb','./models/object.glb','models\\object.glb','models/object\n.glb','models/object\x00.glb',''):
            with self.subTest(value=repr(value)),self.assertRaises(MediaInventoryError):self.inventory([value])

    def test_duplicate_path_rejected(self):
        with self.assertRaises(MediaInventoryError):self.inventory(['models/object.glb']*2)

    def test_file_link_rejected(self):
        (self.root/'models/link.glb').symlink_to(self.file)
        with self.assertRaises(MediaInventoryError):self.inventory(['models/link.glb'])

    def test_ancestor_link_rejected(self):
        (self.root/'alias').symlink_to(self.root/'models',target_is_directory=True)
        with self.assertRaises(MediaInventoryError):self.inventory(['alias/object.glb'])

    def test_root_link_rejected(self):
        (self.root/'root-alias').symlink_to(self.root/'models',target_is_directory=True)
        with self.assertRaises(MediaInventoryError):build_media_inventory(self.root/'root-alias',['object.glb'])

    def test_nonregular_and_missing_rejected(self):
        for name in ('models','models/missing.glb'):
            with self.subTest(name=name),self.assertRaises(MediaInventoryError):self.inventory([name])

    def test_fifo_refused_without_blocking(self):
        os.mkfifo(self.root/'models/pipe')
        with self.assertRaises(MediaInventoryError):self.inventory(['models/pipe'])

    def test_file_changed_during_hash_refused(self):
        real=os.read;changed=False
        def read(fd,n):
            nonlocal changed
            data=real(fd,n)
            if data and not changed:changed=True;self.file.write_bytes(b'changed')
            return data
        with patch('os.read',side_effect=read),self.assertRaises(MediaInventoryError):self.inventory()

    def test_file_replaced_during_hash_refused(self):
        real=os.read;changed=False
        def read(fd,n):
            nonlocal changed
            data=real(fd,n)
            if data and not changed:
                changed=True;self.file.rename(self.root/'old.glb');self.file.write_bytes(data)
            return data
        with patch('os.read',side_effect=read),self.assertRaises(MediaInventoryError):self.inventory()

    def test_ancestor_replaced_during_hash_refused(self):
        real=os.read;changed=False
        def read(fd,n):
            nonlocal changed
            data=real(fd,n)
            if data and not changed:
                changed=True;(self.root/'models').rename(self.root/'old-models');(self.root/'models').mkdir();self.file.write_bytes(data)
            return data
        with patch('os.read',side_effect=read),self.assertRaises(MediaInventoryError):self.inventory()

    def test_errors_do_not_expose_host_paths(self):
        with self.assertRaises(MediaInventoryError) as found:self.inventory(['models/private-missing'])
        self.assertNotIn(str(self.root),str(found.exception));self.assertNotIn('private-missing',str(found.exception))

    def test_unicode_and_spaces_preserved(self):
        name='models/objet épreuve.glb';(self.root/name).write_bytes(b'synthetic')
        self.assertIn(name,self.inventory([name]))

    def test_bundle_contains_only_exact_regular_content(self):
        inv=self.inventory();dest=self.root/'bundle.tar.gz';progress=[]
        create_verified_media_bundle(dest,self.root,list(inv),inv,lambda *args:progress.append(args))
        with tarfile.open(dest,'r:gz') as archive:
            item=archive.getmembers()[0]
            self.assertEqual(item.name,'models/object.glb');self.assertTrue(item.isfile())
            self.assertEqual(item.uid,0);self.assertEqual(item.gid,0);self.assertEqual(item.uname,'');self.assertEqual(item.gname,'')
            self.assertEqual(int(item.mtime),int(self.file.stat().st_mtime))
            self.assertEqual(hashlib.sha256(archive.extractfile(item).read()).hexdigest(),inv[item.name]['sha256'])
        self.assertEqual(progress,[(1,1,'object.glb')])

    def test_bundle_refuses_changed_bytes_even_with_preserved_metadata(self):
        inv=self.inventory();s=self.file.stat();self.file.write_bytes(b'x'*s.st_size);os.utime(self.file,ns=(s.st_atime_ns,s.st_mtime_ns))
        with self.assertRaisesRegex(MediaInventoryError,'content changed'):create_verified_media_bundle(self.root/'bad.tar.gz',self.root,list(inv),inv)

    def test_bundle_refuses_replaced_link_after_inventory(self):
        inv=self.inventory();self.file.unlink();self.file.symlink_to(self.root/'not-media')
        with self.assertRaises(MediaInventoryError):create_verified_media_bundle(self.root/'bad.tar.gz',self.root,list(inv),inv)

    def test_bundle_refuses_mutation_during_read(self):
        inv=self.inventory();real=os.read;changed=False
        def read(fd,n):
            nonlocal changed
            data=real(fd,n)
            if data and not changed:changed=True;self.file.write_bytes(b'changed during read')
            return data
        with patch('os.read',side_effect=read),self.assertRaises(MediaInventoryError):create_verified_media_bundle(self.root/'bad.tar.gz',self.root,list(inv),inv)

    def test_bundle_never_overwrites_existing_output(self):
        inv=self.inventory();dest=self.root/'exists.tar.gz';dest.write_bytes(b'preserved')
        with self.assertRaises(MediaInventoryError):create_verified_media_bundle(dest,self.root,list(inv),inv)
        self.assertEqual(dest.read_bytes(),b'preserved')

    def test_empty_bundle_needs_no_media_root(self):
        dest=self.root/'empty.tar.gz';create_verified_media_bundle(dest,self.root/'absent',[],{})
        with tarfile.open(dest,'r:gz') as archive:self.assertEqual(archive.getmembers(),[])

    def test_bundle_requires_content_identity(self):
        with self.assertRaisesRegex(MediaInventoryError,'identity is missing'):create_verified_media_bundle(self.root/'bad.tar.gz',self.root,['models/object.glb'],{'models/object.glb':{'size_bytes':self.file.stat().st_size}})

    def test_bundle_progress_cannot_hide_changed_later_file(self):
        second=self.root/'models/second.glb';second.write_bytes(b'second synthetic');inv=self.inventory(['models/object.glb','models/second.glb'])
        def change(index,total,name):
            if index==1:second.write_bytes(b'mutated')
        with self.assertRaises(MediaInventoryError):create_verified_media_bundle(self.root/'bad.tar.gz',self.root,list(inv),inv,change)

    def test_inventory_reports_only_verified_files(self):
        events=[];build_media_inventory(self.root,['models/object.glb'],lambda *args:events.append(args))
        self.assertEqual(events,[(1,1,'object.glb')])
        events=[]
        with self.assertRaises(MediaInventoryError):build_media_inventory(self.root,['models/missing'],lambda *args:events.append(args))
        self.assertEqual(events,[])

    def test_invalid_selection_type_is_redacted(self):
        with self.assertRaises(MediaInventoryError):build_media_inventory(self.root,None)

    def test_valid_short_reads_are_buffered_for_tar(self):
        inv=self.inventory();real=os.read;dest=self.root/'short.tar.gz'
        with patch('os.read',side_effect=lambda fd,n:real(fd,min(n,3))):create_verified_media_bundle(dest,self.root,list(inv),inv)
        with tarfile.open(dest,'r:gz') as archive:self.assertEqual(archive.extractfile('models/object.glb').read(),self.file.read_bytes())

    def test_intermediate_ancestor_replacement_refused(self):
        (self.root/'a/b').mkdir(parents=True);target=self.root/'a/b/file.glb';target.write_bytes(b'synthetic')
        real=os.read;changed=False
        def read(fd,n):
            nonlocal changed
            data=real(fd,n)
            if data and not changed:
                changed=True;(self.root/'a').rename(self.root/'old-a');(self.root/'a').mkdir();(self.root/'old-a/b').rename(self.root/'a/b')
            return data
        with patch('os.read',side_effect=read),self.assertRaises(MediaInventoryError):self.inventory(['a/b/file.glb'])


if __name__=='__main__':unittest.main()
